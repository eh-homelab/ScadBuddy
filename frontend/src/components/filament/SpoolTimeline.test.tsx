import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { SpoolEvent, TimelineRow } from '../../api/filament'
import { renderPage } from '../../test/utils'
import { SpoolTimeline } from './SpoolTimeline'

let seq = 0
const row = (values: Partial<SpoolEvent>, links: Partial<Pick<TimelineRow, 'order_id' | 'archive_id'>> = {}): TimelineRow => ({
  event: {
    id: `e${++seq}`,
    seq,
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
  },
  order_id: null,
  archive_id: null,
  ...links,
})

const catalogs = [[{ id: 'shelf', name: 'Workshop / Shelf A' }, { id: 'box', name: 'Workshop / Dry box' }]]
const types = [
  { id: 't-black', code: 'BL-PLA-BASIC-BLACK' },
  { id: 't-pink', code: 'BL-PLA-BASIC-HOT-PINK' },
]

const rows = [
  row({ at: '2026-11-12T10:00:00Z', kind: 'edited', actor: 'service:agent', data: { changes: { cost_per_kg: ['20.0000', '25.0000'], type_id: ['t-black', 't-pink'] }, author: { session: 's-1' } } }),
  row({ at: '2026-11-11T10:00:00Z', kind: 'print_ended', source: 'bb', delta_g: -55, grams_after: 940, actor: 'bambuddy' }, { archive_id: 41 }),
  row({ at: '2026-11-10T10:00:00Z', kind: 'print_ended', source: 'print_actual', delta_g: -35, actor: 'system:printer-watch' }, { order_id: 'order-7' }),
  row({ at: '2026-11-09T10:00:00Z', kind: 'weighed', source: 'scale', grams_after: 995, actor: 'person@example.com' }),
  row({ at: '2026-11-08T10:00:00Z', kind: 'moved', data: { from: 'shelf', to: 'box' }, actor: 'person@example.com' }),
  row({ at: '2026-11-07T10:00:00Z', kind: 'tag_bound', data: { tag_id: 'tag-1' }, actor: 'person@example.com' }),
  row({ at: '2026-11-01T10:00:00Z', kind: 'created', actor: 'person@example.com' }),
]

const items = () => within(screen.getByRole('list', { name: 'History of this spool' })).getAllByRole('listitem').filter((item) => item.parentElement?.getAttribute('aria-label') === 'History of this spool')

describe('the spool timeline', () => {
  it('lists each change of an edit and who made it', () => {
    renderPage(<SpoolTimeline rows={rows} catalogs={catalogs} types={types} />)
    const edit = items()[0]!
    expect(within(within(edit).getByRole('list', { name: 'Changes' })).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'Cost per kg: 20.00 → 25.00',
      'Type: BL-PLA-BASIC-BLACK → BL-PLA-BASIC-HOT-PINK',
    ])
    expect(edit).toHaveTextContent('Assistant')
    expect(items()[1]).toHaveTextContent('Bambuddy sync')
    expect(items()[2]).toHaveTextContent('Printer')
    expect(items()[4]).toHaveTextContent('from Workshop / Shelf A to Workshop / Dry box')
  })

  it('links a plate to its order and to its print', () => {
    renderPage(<SpoolTimeline rows={rows} catalogs={catalogs} types={types} />)
    expect(within(items()[1]!).getByRole('link', { name: 'Print 41' })).toHaveAttribute('href', '/prints/41')
    expect(within(items()[2]!).getByRole('link', { name: 'Its order' })).toHaveAttribute('href', '/orders/order-7')
  })

  it("links each id an event names to that entity's page (#6869)", () => {
    const hrefs = new Map([
      ['t-black', '/filament/types/t-black'],
      ['t-pink', '/filament/types/t-pink'],
      ['shelf', '/filament/spools?location=Shelf+A'],
    ])
    renderPage(<SpoolTimeline rows={rows} catalogs={catalogs} types={types} hrefs={hrefs} />)
    const changes = within(items()[0]!).getByRole('list', { name: 'Changes' })
    expect(within(changes).getByRole('link', { name: 'BL-PLA-BASIC-BLACK' })).toHaveAttribute('href', '/filament/types/t-black')
    expect(within(changes).getByRole('link', { name: 'BL-PLA-BASIC-HOT-PINK' })).toHaveAttribute('href', '/filament/types/t-pink')
    expect(within(changes).getAllByRole('listitem').map((item) => item.textContent)).toEqual(['Cost per kg: 20.00 → 25.00', 'Type: BL-PLA-BASIC-BLACK → BL-PLA-BASIC-HOT-PINK'])
    const moved = items()[4]!
    expect(within(moved).getByRole('link', { name: 'Workshop / Shelf A' })).toHaveAttribute('href', '/filament/spools?location=Shelf+A')
    // A place with no page in the map stays text.
    expect(within(moved).queryByRole('link', { name: 'Workshop / Dry box' })).toBeNull()
    expect(moved).toHaveTextContent('from Workshop / Shelf A to Workshop / Dry box')
  })

  it('narrows the rows to the kind the filter chooses', async () => {
    const { user } = renderPage(<SpoolTimeline rows={rows} catalogs={catalogs} types={types} />)
    expect(items()).toHaveLength(7)
    const kinds = { Prints: ['Print ended', 'Print ended'], Weighings: ['Weighed'], Edits: ['Edited'], Moves: ['Moved'], Tags: ['Tag bound'], All: ['Edited', 'Print ended', 'Print ended', 'Weighed', 'Moved', 'Tag bound', 'Added'] }
    for (const [filter, shown] of Object.entries(kinds)) {
      await user.click(screen.getByRole('button', { name: filter }))
      expect(screen.getByRole('button', { name: filter })).toHaveAttribute('aria-pressed', 'true')
      expect(items().map((item) => item.querySelector('.font-medium')?.textContent)).toEqual(shown)
    }
  })

  it('says what a weighing with a breakdown read and how it compares with the ledger (#7682)', () => {
    const breakdown = { reading_g: 1180, empty_g: 200, empty_source: 'insert', empty_basis: 'catalog', empty_name: 'Plastic' }
    renderPage(
      <SpoolTimeline
        rows={[
          row({ at: '2026-11-12T10:00:00Z', kind: 'weighed', source: 'scale', grams_after: 980, data: { ...breakdown, expected_g: 992, drift_g: -12 } }),
          row({ at: '2026-11-11T10:00:00Z', kind: 'weighed', source: 'scale', grams_after: 992, data: { ...breakdown, expected_g: 991.6, drift_g: 0.4 } }),
          row({ at: '2026-11-10T10:00:00Z', kind: 'weighed', source: 'scale', grams_after: 1000, data: { ...breakdown, expected_g: 992, drift_g: 8 } }),
          row({ at: '2026-11-09T10:00:00Z', kind: 'weighed', source: 'scale', grams_after: 980, data: { ...breakdown, expected_g: null, drift_g: null } }),
        ]}
        catalogs={catalogs}
        types={types}
      />,
    )
    expect(items()[0]).toHaveTextContent('Weighed, 980 g, scale, reading 1180 g, spool 200 g, 12 g less than expected')
    expect(items()[1]).toHaveTextContent('Weighed, 992 g, scale, reading 1180 g, spool 200 g, matches the ledger')
    expect(items()[2]).toHaveTextContent('Weighed, 1000 g, scale, reading 1180 g, spool 200 g, 8 g more than expected')
    expect(items()[3]).toHaveTextContent('Weighed, 980 g, scale, reading 1180 g, spool 200 g')
    expect(items()[3]).not.toHaveTextContent(/expected|ledger/)
  })

  it("says how a reading differs from the last value, in words", () => {
    renderPage(<SpoolTimeline rows={[row({ kind: 'weighed', source: 'scale', grams_after: 820, delta_g: -20 })]} catalogs={catalogs} types={types} />)
    expect(items()[0]).toHaveTextContent('Weighed, 820 g, scale, 20 g less than the last value')
  })

  it('dots a reading in the accent the chart draws readings in, and a print in ink, as the chart ticks plates', () => {
    renderPage(<SpoolTimeline rows={rows} catalogs={catalogs} types={types} />)
    const dot = (item: HTMLElement) => item.querySelector('span[aria-hidden="true"]')!
    expect(dot(items()[3]!)).toHaveClass('bg-primary')
    expect(dot(items()[1]!)).toHaveClass('bg-foreground')
    expect(dot(items()[1]!)).not.toHaveClass('bg-primary')
  })
})
