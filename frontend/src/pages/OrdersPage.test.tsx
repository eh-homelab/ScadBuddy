import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT } from '../mocks/features/orders/state'
import { choose } from '../test/select'
import { renderPage } from '../test/utils'
import { OrdersPage } from './OrdersPage'

function Location() {
  const location = useLocation()
  return <output data-testid="location">{`${location.pathname}${location.search}`}</output>
}

describe('OrdersPage (#4249)', () => {
  beforeEach(() => resetMockState())

  it('lists orders newest first with their customer and status', async () => {
    renderPage(
      <>
        <OrdersPage />
        <Location />
      </>,
      { route: '/orders' },
    )
    const table = await screen.findByRole('table', { name: 'Orders' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((r) => within(r).getByRole('link').textContent)).toEqual(['ORD-2026-3', 'ORD-2026-2', 'ORD-2026-1'])
    expect(within(screen.getByTestId(`order-row-${ORDER_ACCEPTED}`)).getByText('Ada Lovelace')).toBeInTheDocument()
    expect(within(screen.getByTestId(`order-row-${ORDER_ACCEPTED}`)).getByText('Accepted')).toBeInTheDocument()
  })

  it('keeps the status chip and the customer in the URL', async () => {
    const { user } = renderPage(
      <>
        <OrdersPage />
        <Location />
      </>,
      { route: '/orders' },
    )
    await screen.findByRole('table', { name: 'Orders' })
    await user.click(screen.getByRole('radio', { name: 'Draft' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/orders?status=draft')
    await waitFor(() => expect(screen.getByTestId(`order-row-${ORDER_DRAFT}`)).toBeInTheDocument())
    expect(screen.queryByTestId(`order-row-${ORDER_ACCEPTED}`)).toBeNull()
    await choose(user, screen.getByRole('combobox', { name: 'Customer' }), 'Bob Builder')
    expect(screen.getByTestId('location')).toHaveTextContent('status=draft&customer=c1a00000-0000-4000-8000-000000000002')
    await waitFor(() => expect(screen.getByText('No orders match.')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(screen.getByTestId('location')).toHaveTextContent(/\/orders$/)
  })

  it('makes a new order for a customer and moves to it', async () => {
    const { user } = renderPage(
      <>
        <OrdersPage />
        <Location />
      </>,
      { route: '/orders' },
    )
    await screen.findByRole('table', { name: 'Orders' })
    await user.click(screen.getByRole('button', { name: 'New order' }))
    await choose(user, screen.getByRole('combobox', { name: 'Customer for the new order' }), 'Bob Builder')
    await user.click(screen.getByRole('button', { name: 'Create order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
  })
})
