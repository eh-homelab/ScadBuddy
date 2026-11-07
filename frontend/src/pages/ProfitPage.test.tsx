import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_CLOSED, ORDER_DRAFT } from '../mocks/features/orders/state'
import { renderPage } from '../test/utils'
import { OrdersPage } from './OrdersPage'
import { periodOf, rowLink } from '../components/orders/profit'
import { ProfitPage } from './ProfitPage'

function Location() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

/** The mock's two accepted orders were accepted in October 2026. */
const ROUTE = '/orders/profit?from=2026-10-01&to=2026-10-31'
const PERIOD = periodOf('2026-10-01', '2026-10-31')

describe('ProfitPage (#5329)', () => {
  beforeEach(() => resetMockState())

  it('asks for the period picked, the last day included', () => {
    expect(PERIOD).toEqual({ from: '2026-10-01T00:00:00Z', to: '2026-11-01T00:00:00.000Z' })
    expect(periodOf('', '')).toEqual({ from: undefined, to: undefined })
  })

  it('shows a row per product and currency, with a total, and tints a loss', async () => {
    const report = await api.profitReport(PERIOD)
    const model = report.rows.find((r) => r.group === 'model')
    if (!model) throw new Error('the mocks hold no model row')
    await api.addCostEntry(ORDER_ACCEPTED, { kind: 'other', amount_minor: 1_000_000 })

    renderPage(<ProfitPage />, { route: ROUTE })

    const table = await screen.findByRole('table', { name: 'Profit by model' })
    const total = screen.getByTestId('profit-row-USD-total')
    expect(total).toHaveTextContent('Total USD')
    // The cost entry is on the order, not on an item: the order's own row is the loss.
    const own = screen.getByTestId('profit-row-USD-order')
    expect(own.className).toContain('text-destructive')
    expect(total.className).toContain('text-destructive')
    const row = screen.getByTestId(`profit-row-USD-model-${model.key}`)
    expect(row).toHaveTextContent(String(model.key))
    expect(within(table).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Product', 'Orders', 'Units', 'Charged', 'Real cost', 'Profit', 'Margin', 'Waste'])
  })

  it('links a row to the orders behind it', async () => {
    const report = await api.profitReport(PERIOD)
    const model = report.rows.find((r) => r.group === 'model')
    if (!model) throw new Error('the mocks hold no model row')
    const href = rowLink(report, model)
    const params = new URL(href, 'http://x').searchParams
    expect(params.get('product')).toBe(model.product)
    expect(params.get('accepted_from')).toBe(report.period_from)
    expect(params.get('accepted_to')).toBe(report.period_to)
    expect(params.get('currency')).toBe('USD')

    const { user } = renderPage(
      <>
        <ProfitPage />
        <Location />
      </>,
      { route: ROUTE },
    )
    const row = await screen.findByTestId(`profit-row-USD-model-${model.key}`)
    await user.click(within(row).getByRole('link'))
    expect(screen.getByTestId('location')).toHaveTextContent(href)
  })

  it('lists only the row’s orders, and Show every order clears the row’s filters', async () => {
    const report = await api.profitReport(PERIOD)
    const model = report.rows.find((r) => r.group === 'model')
    if (!model) throw new Error('the mocks hold no model row')
    const { user } = renderPage(
      <>
        <OrdersPage />
        <Location />
      </>,
      { route: rowLink(report, model) },
    )

    const notice = await screen.findByTestId('orders-report-filter')
    expect(notice).toHaveTextContent(`model ${model.key}`)
    expect(notice).toHaveTextContent('in USD')
    await screen.findByRole('table', { name: 'Orders' })
    const shown = [ORDER_ACCEPTED, ORDER_CLOSED].filter((id) => screen.queryByTestId(`order-row-${id}`))
    expect(shown.length).toBe(model.orders)
    // The draft was never accepted: in no period.
    expect(screen.queryByTestId(`order-row-${ORDER_DRAFT}`)).toBeNull()

    await user.click(screen.getByRole('button', { name: 'Show every order' }))
    expect(screen.getByTestId('location')).toHaveTextContent(/\/orders$/)
    await waitFor(() => expect(screen.getByTestId(`order-row-${ORDER_DRAFT}`)).toBeInTheDocument())
  })

  it('says when the report stopped at its cap', async () => {
    const real = await api.profitReport(PERIOD)
    const spy = vi.spyOn(api, 'profitReport').mockResolvedValue({ ...real, complete: false, read_from: '2026-10-18T13:00:00Z', max_orders: 2000 })
    renderPage(<ProfitPage />, { route: ROUTE })
    expect(await screen.findByRole('alert')).toHaveTextContent('reads at most 2000 orders')
    spy.mockRestore()
  })

  it('keeps the period in the URL', async () => {
    const spy = vi.spyOn(api, 'profitReport')
    renderPage(
      <>
        <ProfitPage />
        <Location />
      </>,
      { route: ROUTE },
    )
    await screen.findByRole('table', { name: 'Profit by model' })
    fireEvent.change(screen.getByLabelText('Accepted from'), { target: { value: '2026-10-18' } })
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('from=2026-10-18'))
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(periodOf('2026-10-18', '2026-10-31')))
    spy.mockRestore()
  })
})
