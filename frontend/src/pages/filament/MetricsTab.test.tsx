import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { T } from '../../mocks/features/filament/seed'
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

  it('shows the stats by material number, each type linked, the untyped spools last (#7152)', async () => {
    render()
    const table = await within(region('By material number')).findByRole('table', { name: 'Stats by material number' })
    const rows = within(table).getAllByRole('row').slice(1)
    const jade = rows.find((row) => row.dataset.key === 'BL-PLA-BASIC-JADE-WHITE')!
    expect(within(jade).getByRole('link', { name: 'BL-PLA-BASIC-JADE-WHITE' })).toHaveAttribute('href', `/filament/types/${T.JADE}`)
    expect(rows.at(-1)).toHaveAttribute('data-key', '')
    expect(within(rows.at(-1)!).getByText('No material number')).toBeInTheDocument()
  })

  it('asks the material number stats for the range on screen', async () => {
    const seen: URLSearchParams[] = []
    server.events.on('request:start', ({ request }) => {
      const url = new URL(request.url)
      if (url.pathname === '/api/v1/filament/metrics/material-numbers') seen.push(url.searchParams)
    })
    const { user } = render()
    await within(region('By material number')).findByRole('table', { name: 'Stats by material number' })
    await user.click(within(screen.getByRole('group', { name: 'Range' })).getByRole('button', { name: '90 days' }))
    await waitFor(() => expect(seen.map((one) => one.get('since'))).toEqual(['2026-05-22', '2026-08-20']))
    expect(seen.every((one) => one.get('until') === '2026-11-18')).toBe(true)
  })

  it('opens the consumption table behind "Show the numbers"', async () => {
    const { user } = render()
    await within(region('Consumption')).findByRole('img', { name: /140 g in all/ })
    expect(screen.queryByRole('table', { name: 'Consumption' })).not.toBeInTheDocument()
    await user.click(within(region('Consumption')).getByText('Show the numbers'))
    const table = await screen.findByRole('table', { name: 'Consumption' })
    const petg = within(table).getByText('PETG').closest('tr')
    expect(petg).not.toBeNull()
    expect(within(petg as HTMLElement).getByText('120 g')).toBeInTheDocument()
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
    expect(within(row).getAllByText('Reorder now').length).toBeGreaterThan(0)
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

  it("keeps a value's colour when the range changes: colours follow the 365-day rank (R-T8-1)", async () => {
    // PLA uses most in the last 90 days, PETG over the year: ranked by the range, PLA would be blue at 90 days and orange at 365.
    server.use(
      http.get('*/api/v1/filament/metrics', ({ request }) => {
        const url = new URL(request.url)
        const since = url.searchParams.get('since')!
        const consumption = [
          ...(since < '2026-04-01' ? [{ bucket: '2026-03-01', key: 'petg', label: 'PETG', used_g: 450, wasted_g: 0 }] : []),
          { bucket: '2026-11-01', key: 'petg', label: 'PETG', used_g: 50, wasted_g: 0 },
          { bucket: '2026-11-01', key: 'pla', label: 'PLA', used_g: 100, wasted_g: 0 },
        ]
        return HttpResponse.json({ currency: 'USD', dimension: 'material', bucket: 'month', since, until: url.searchParams.get('until'), stock: [], consumption, waste: [] })
      }),
    )
    const swatch = async (name: string) => {
      const legend = await within(region('Consumption')).findByRole('list', { name: 'Consumption by material' })
      return within(legend).getByText(name).closest('li')!.querySelector('span[aria-hidden="true"]')!.getAttribute('class')
    }
    const { user } = render('/filament/metrics?range=90d')
    await within(region('Consumption')).findByRole('img', { name: /150 g in all/ })
    const pla = await swatch('PLA')
    const petg = await swatch('PETG')
    expect(petg).toContain('text-series-1')
    await user.click(within(screen.getByRole('group', { name: 'Range' })).getByRole('button', { name: '365 days' }))
    await within(region('Consumption')).findByRole('img', { name: /600 g in all/ })
    expect(await swatch('PLA')).toBe(pla)
    expect(await swatch('PETG')).toBe(petg)
  })

  it('draws the consumption as soon as the range request returns, before the 365-day rank does (R-T8-12)', async () => {
    let release!: () => void
    const held = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.get('*/api/v1/filament/metrics', async ({ request }) => {
        const url = new URL(request.url)
        const since = url.searchParams.get('since')!
        const until = url.searchParams.get('until')!
        if (Date.parse(until) - Date.parse(since) > 300 * 86_400_000) await held
        return HttpResponse.json({
          currency: 'USD',
          dimension: 'material',
          bucket: 'month',
          since,
          until,
          stock: [],
          consumption: [{ bucket: '2026-11-01', key: 'pla', label: 'PLA', used_g: 100, wasted_g: 0 }],
          waste: [],
        })
      }),
    )
    render()
    expect(await within(region('Consumption')).findByRole('img', { name: /100 g in all/ })).toBeInTheDocument()
    release()
  })

  describe('a price line', () => {
    const zone = process.env.TZ
    beforeEach(() => {
      server.use(
        http.get('*/api/v1/filament/metrics/purchasing', () =>
          HttpResponse.json({
            prices: [
              {
                type_id: T.PINK,
                type_label: 'BL-PLA-BASIC-HOT-PINK',
                supplier_id: SUPPLIER.BAMBU,
                supplier_name: 'Bambu Lab',
                currency: 'USD',
                points: [
                  { on_date: '2026-10-07', price_per_kg: '15.91' },
                  { on_date: '2026-11-10', price_per_kg: '17.305' },
                ],
              },
            ],
            spend: [],
          }),
        ),
      )
    })
    afterEach(() => {
      if (zone === undefined) delete process.env.TZ
      else process.env.TZ = zone
    })

    const chart = () => within(region('Prices')).findByRole('img', { name: /^BL-PLA-BASIC-HOT-PINK at Bambu Lab/ })
    /** The Hot Pink chart, with the pointer at `clientX` of its 320-wide box. */
    async function hover(clientX: number) {
      const svg = await chart()
      svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: 320, height: 90, right: 320, bottom: 90, x: 0, y: 0, toJSON: () => ({}) })
      fireEvent.pointerMove(svg, { clientX, clientY: 40 })
      return svg
    }
    /** The hover tooltip: the chart's one `aria-hidden` box outside its SVG. */
    const tip = () => [...region('Prices').querySelectorAll('div[aria-hidden="true"]')].find((one) => one.closest('svg') === null)

    it('shows the decimal price in the tooltip, as the headline does (R-T8-2)', async () => {
      render()
      await hover(320)
      await waitFor(() => expect(tip()).toHaveTextContent('$17.305/kg'))
      expect(within(region('Prices')).getAllByText('$17.305/kg')).toHaveLength(2)
    })

    it('dates a price on its calendar day for a viewer west of UTC (R-T8-3)', async () => {
      process.env.TZ = 'America/New_York'
      render()
      const svg = await hover(0)
      await waitFor(() => expect(tip()).toHaveTextContent('Oct 7, 2026'))
      expect(tip()).not.toHaveTextContent('Oct 6')
      expect([...svg.querySelectorAll('text')].map((one) => one.textContent)).toContain('Oct 7')
    })

    it("holds the last price on to the range's last day (R-T8-7)", async () => {
      render()
      const svg = await chart()
      // After its last change the step goes level to the plot's right edge (320 - 8), the range's `until`.
      expect(svg.querySelector('path[data-line]')!.getAttribute('d')).toMatch(/V[\d.]+ H312\.0$/)
      expect([...svg.querySelectorAll('text')].map((one) => one.textContent)).toContain('Nov 18')
    })
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
