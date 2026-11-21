import { screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { listSpools } from '../../api/filament'
import { T } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { SpoolForecast } from './SpoolForecast'

// The burn and the days depend on today: the values below are what the backend's own
// `metrics.burn`, `empty_on` and `reorder_on` give for the seed's plates on this day.
beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] }).setSystemTime('2026-11-18T12:00:00Z')
})
afterEach(() => {
  vi.useRealTimers()
})

async function render() {
  const spools = (await listSpools()).filter((spool) => !spool.archived_at)
  return renderPage(<SpoolForecast spools={spools} />)
}

describe('the forecast view (#6269)', () => {
  it('lists the spools by the day each runs out, the ones with none last by number', async () => {
    await render()
    const table = await screen.findByRole('table', { name: 'Spool forecast' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((row) => row.dataset.spool)).toEqual(['2', '3', '1', '4', '5', '6', '7', '8', '9', '10', '11'])
    // #2: 120 g over its 17.1 days since it was added; #3: its 20 g plate over the 3.2 days since it was added.
    expect(within(rows[0]!).getByText('7 g/day')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('Mar 15, 2027')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('6.2 g/day')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('Apr 16, 2027')).toBeInTheDocument()
    expect(within(rows[2]!).getByText('No prints in 30 days')).toBeInTheDocument()
  })

  it('lists the types too, and says "Reorder now" for one whose reorder day is past', async () => {
    await render()
    const types = await screen.findByRole('table', { name: 'Days of stock by type' })
    const rows = within(types).getAllByRole('row').slice(1)
    // Reorder now first: the orange type runs out on 2027-03-15, and its 30 days of lead time and 90 of margin put its reorder day on 2026-11-15.
    expect(rows[0]!.dataset.type).toBe(T.ORANGE)
    // The badge is in the "Reorder by" cell, and (shown only where the table's container is narrower than the table's 44 rem) under the type's name (R-T8-6, R-T8-12).
    const cells = within(rows[0]!).getAllByRole('cell')
    expect(within(cells[0]!).getByText('Reorder now')).toBeInTheDocument()
    expect(within(cells.at(-1)!).getByText('Reorder now')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('117 days')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('30 d lead, 90 d margin')).toBeInTheDocument()
    // The pink type: 7 days of lead time and 3 of margin, the settings', before 2027-04-16.
    expect(rows[1]!.dataset.type).toBe(T.PINK)
    expect(within(rows[1]!).getByText('Apr 6, 2027')).toBeInTheDocument()
    expect(rows.filter((row) => within(row).queryAllByText('Reorder now').length > 0)).toHaveLength(1)
  })
})
