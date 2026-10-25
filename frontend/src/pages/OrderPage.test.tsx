import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { CUSTOMER_BOB, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT } from '../mocks/features/orders/state'
import { renderPage } from '../test/utils'
import { OrderPage } from './OrderPage'

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${id}` },
  )
}

describe('OrderPage (#4249)', () => {
  beforeEach(() => resetMockState())

  it('shows the order, its items and totals', async () => {
    render(ORDER_ACCEPTED)
    expect(await screen.findByRole('heading', { name: 'ORD-2026-2' })).toBeInTheDocument()
    const items = screen.getByRole('table', { name: 'Line items' })
    expect(within(items).getByRole('button', { name: 'Name Keychain' })).toBeInTheDocument()
    expect(within(items).getByText('Approved')).toBeInTheDocument()
    const totals = screen.getByTestId('order-totals')
    expect(totals).toHaveTextContent('30.00')
    expect(totals).toHaveTextContent('35.00')
    expect(screen.getByRole('heading', { name: 'History' })).toBeInTheDocument()
  })

  it("shows the lifecycle's refusal naming the items in the way", async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(screen.getByRole('button', { name: 'Accept order' }))
    const alert = await screen.findByTestId('order-refusal')
    expect(alert).toHaveTextContent(/no accepted quote/)
    expect(alert).toHaveTextContent('Name Keychain')
  })

  it('prices an item from its drawer, accepts the order, and closes it from a shipment', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(screen.getByTestId(`order-item-${ITEM_DRAFT}`)).getByRole('button', { name: 'Name Keychain' }))
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    await user.type(within(drawer).getByLabelText(/Price per unit/), '12.50')
    await user.click(within(drawer).getByRole('button', { name: 'Record and accept' }))
    await waitFor(() => expect(within(drawer).getByText('Accepted')).toBeInTheDocument())
    await user.click(within(drawer).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.getByText('Quoted')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Accept order' }))
    await waitFor(() => expect(screen.getByText('Accepted')).toBeInTheDocument())
    expect(screen.getByTestId('order-totals')).toHaveTextContent('25.00')
    // Record and accept an attempt, ship it, close.
    await user.click(within(screen.getByTestId(`order-item-${ITEM_DRAFT}`)).getByRole('button', { name: 'Name Keychain' }))
    const again = await screen.findByRole('dialog', { name: 'Name Keychain' })
    await user.click(within(again).getByRole('button', { name: 'Record attempt' }))
    await user.click(await within(again).findByRole('button', { name: 'Accept attempt 1' }))
    await waitFor(() => expect(within(again).getByText('Fulfilled')).toBeInTheDocument())
    await user.click(within(again).getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.getByText('Ready')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Add shipment' }))
    await waitFor(() => expect(screen.getByText('Shipped')).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Close order' }))
    await waitFor(() => expect(screen.getByText('Closed')).toBeInTheDocument())
  })

  it('adds an item from the order page while the order is open, and only then (#4251)', async () => {
    const order = await api.createOrder({ customer_id: CUSTOMER_BOB })
    const { user } = render(order.id)
    await screen.findByRole('heading', { name: order.number })
    expect(screen.getByText(/Add one with Add item above/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add item' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: 'Current version, default settings' })).toBeChecked())
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add item' })).not.toBeInTheDocument())
    expect(await within(screen.getByRole('table', { name: 'Line items' })).findByRole('button', { name: 'Ball Maze' })).toBeInTheDocument()
  })

  it('points an empty table at Add item only where there is one', async () => {
    const order = await api.createOrder({ customer_id: CUSTOMER_BOB })
    await api.cancelOrder(order.id)
    render(order.id)
    await screen.findByRole('heading', { name: order.number })
    expect(screen.getByText('No items.')).toBeInTheDocument()
    expect(screen.queryByText(/Add one with Add item above/)).not.toBeInTheDocument()
  })

  it('offers no Add item once the order is accepted', async () => {
    render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    expect(screen.queryByRole('button', { name: 'Add item' })).not.toBeInTheDocument()
  })
})
