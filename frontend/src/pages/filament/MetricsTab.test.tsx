import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { bucketsBetween } from '../../components/filament/metricsQuery'
import { MetricsTab } from './MetricsTab'

// The burn, and so the days of stock, depend on today: fixed at the day the backend's own
// `metrics` functions were run on the seed's plates (SpoolForecast.test.tsx).
beforeEach(() => {
  window.localStorage.clear()
  vi.useFakeTimers({ toFake: ['Date'] }).setSystemTime('2026-11-18T12:00:00Z')
})
afterEach(() => {
  vi.useRealTimers()
})

const render = (route = '/filament/metrics') => renderPage(<MetricsTab />, { route })
const region = (name: string) => screen.getByRole('region', { name })

/** The query of every `GET /filament/metrics` the tab sent. */
function metricsRequests(): URLSearchParams[] {
  const seen: URLSearchParams[] = []
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url)
    if (url.pathname === '/api/v1/filament/metrics') seen.push(url.searchParams)
  })
  return seen
}

afterEach(() => server.events.removeAllListeners())

describe('the Metrics tab (#6269)', () => {
  it('shows the stock, consumption, waste, prices, spend and days of stock from the mock', async () => {
    render()
    expect(await within(region('Stock')).findByRole('list', { name: 'Stock by material' })).toBeInTheDocument()
    const stock = within(region('Stock')).getByRole('list', { name: 'Stock by material' })
    expect(within(stock).getByText('PLA')).toBeInTheDocument()
    // #2's 120 g (PETG) and #3's 20 g (PLA), by the one plate rule.
    const consumption = region('Consumption')
    expect(within(consumption).getByRole('img', { name: /^Grams used per month .* by material: PETG 120 g, PLA 20 g\. 140 g in all, 30 g of it wasted\.$/ })).toBeInTheDocument()
    const waste = within(region('Waste')).getByRole('list', { name: 'Waste by printer' })
    expect(within(waste).getByText('3DP-31B-598')).toBeInTheDocument()
    expect(within(waste).getByText('25% of 120 g')).toBeInTheDocument()
    expect(await within(region('Prices')).findByRole('list', { name: 'Price per kg' })).toBeInTheDocument()
    expect(within(region('Prices')).getByRole('img', { name: /^BL-PLA-BASIC-JADE-WHITE at Bambu Lab, price per kg/ })).toBeInTheDocument()
    expect(within(region('Spend')).getByRole('table', { name: 'Spend by supplier and month' })).toBeInTheDocument()
    expect(within(region('Days of stock')).getByRole('table', { name: 'Days of stock by type' })).toBeInTheDocument()
  })

  it('asks again with dimension=brand when the grouping changes to Brand', async () => {
    const seen = metricsRequests()
    const { user } = render()
    await within(region('Consumption')).findByRole('list', { name: 'Consumption by material' })
    expect(seen.at(-1)?.get('dimension')).toBe('material')
    await choose(user, screen.getByRole('combobox', { name: 'Group by' }), 'By brand')
    const legend = await within(region('Consumption')).findByRole('list', { name: 'Consumption by brand' })
    expect(within(legend).getByText('Bambu Lab')).toBeInTheDocument()
    expect(seen.at(-1)?.get('dimension')).toBe('brand')
    expect(seen.at(-1)?.get('since')).toBe('2026-05-22')
    expect(seen.at(-1)?.get('until')).toBe('2026-11-18')
  })

  it('shows each currency of the spend apart, with a total for each', async () => {
    server.use(
      http.get('*/api/v1/filament/metrics/purchasing', () =>
        HttpResponse.json({
          prices: [],
          spend: [
            { supplier_id: SUPPLIER.BAMBU, supplier_name: 'Bambu Lab', month: '2026-10-01', currency: 'EUR', total: '12.0000', invoices: 1 },
            { supplier_id: SUPPLIER.BAMBU, supplier_name: 'Bambu Lab', month: '2026-10-01', currency: 'USD', total: '34.6100', invoices: 2 },
          ],
        }),
      ),
    )
    render()
    const table = await within(region('Spend')).findByRole('table', { name: 'Spend by supplier and month' })
    const rows = within(table).getAllByRole('row').filter((row) => row.dataset.currency)
    expect(rows.map((row) => row.dataset.currency)).toEqual(['EUR', 'USD'])
    expect(within(rows[0]!).getByText('€12.00')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('$34.61')).toBeInTheDocument()
    expect(within(table).getByText('Total in EUR')).toBeInTheDocument()
    expect(within(table).getByText('Total in USD')).toBeInTheDocument()
  })

  it('says "Reorder now" for the type whose reorder day is past', async () => {
    render()
    const table = await within(region('Days of stock')).findByRole('table', { name: 'Days of stock by type' })
    const row = within(table).getByRole('link', { name: 'BL-PETG-HF-TRANSLUCENT-ORANGE' }).closest('tr')!
    expect(within(row).getByText('Reorder now')).toBeInTheDocument()
  })

  it('keeps the view across a remount while "Remember my view" is on', async () => {
    const { user, unmount } = render()
    await user.click(await screen.findByRole('switch', { name: 'Remember my view' }))
    await user.click(within(screen.getByRole('group', { name: 'Range' })).getByRole('button', { name: '90 days' }))
    await user.click(within(screen.getByRole('group', { name: 'Columns per' })).getByRole('button', { name: 'Weeks' }))
    unmount()
    render()
    await waitFor(() => expect(within(screen.getByRole('group', { name: 'Range' })).getByRole('button', { name: '90 days' })).toHaveAttribute('aria-pressed', 'true'))
    expect(within(screen.getByRole('group', { name: 'Columns per' })).getByRole('button', { name: 'Weeks' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('opens the default view after a remount while "Remember my view" is off', async () => {
    const { user, unmount } = render()
    await user.click(within(await screen.findByRole('group', { name: 'Range' })).getByRole('button', { name: '90 days' }))
    unmount()
    render()
    expect(within(await screen.findByRole('group', { name: 'Range' })).getByRole('button', { name: '180 days' })).toHaveAttribute('aria-pressed', 'true')
  })
})

describe('the consumption buckets', () => {
  it('starts a week on Monday and a month on its first day, through the last day', () => {
    expect(bucketsBetween('2026-11-04', '2026-11-18', 'week')).toEqual(['2026-11-02', '2026-11-09', '2026-11-16'])
    expect(bucketsBetween('2026-09-15', '2026-11-18', 'month')).toEqual(['2026-09-01', '2026-10-01', '2026-11-01'])
  })
})
