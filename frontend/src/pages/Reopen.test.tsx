import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it } from 'vitest'
import { resetMockState } from '../mocks/handlers'
import { CUSTOMER_ADA, ITEM_DRAFT, ORDER_CLOSED, ORDER_DRAFT, state } from '../mocks/features/orders/state'
import { renderPage } from '../test/utils'
import { CustomerPage } from './CustomerPage'
import { OrderPage } from './OrderPage'

function order(id: string) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${id}` },
  )
}

describe('undoing an archive, a close and a cancel', () => {
  beforeEach(() => resetMockState())

  it('unarchives a customer', async () => {
    const { user } = renderPage(
      <Routes>
        <Route path="/customers/:id" element={<CustomerPage />} />
      </Routes>,
      { route: `/customers/${CUSTOMER_ADA}` },
    )
    await screen.findByRole('heading', { name: 'Ada Lovelace' })
    await user.click(screen.getByRole('button', { name: 'Archive' }))
    await user.click(await screen.findByRole('button', { name: 'Unarchive' }))
    expect(await screen.findByRole('button', { name: 'Archive' })).toBeInTheDocument()
    expect(state.customers.find((c) => c.id === CUSTOMER_ADA)?.archived_at).toBeNull()
  })

  it('reopens a closed order to what its rows say', async () => {
    const { user } = order(ORDER_CLOSED)
    await screen.findByRole('heading', { name: 'ORD-2026-1' })
    await user.click(screen.getByRole('button', { name: 'Reopen order' }))
    await waitFor(() => expect(state.orders.find((o) => o.id === ORDER_CLOSED)?.status).toBe('shipped'))
    expect(await screen.findByRole('button', { name: 'Close order' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Reopen order' })).not.toBeInTheDocument()
  })

  it('reopens a cancelled line item from its drawer', async () => {
    const { user } = order(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(screen.getByTestId(`order-item-${ITEM_DRAFT}`)).getByRole('button', { name: 'Name Keychain' }))
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    // A draft item deletes rather than cancels; quote it first so it cancels.
    await user.type(within(drawer).getByLabelText(/Price per unit/), '12.50')
    await user.click(within(drawer).getByRole('button', { name: 'Record and accept' }))
    await user.click(await within(drawer).findByRole('button', { name: 'Cancel item' }))
    await user.click(await within(drawer).findByRole('button', { name: 'Reopen item' }))
    await waitFor(() => expect(state.items.find((i) => i.id === ITEM_DRAFT)?.status).toBe('approved'))
    expect(await within(drawer).findByRole('button', { name: 'Cancel item' })).toBeInTheDocument()
    expect(state.events.filter((e) => e.kind === 'item.reopened').map((e) => e.detail)).toEqual([{ item_id: ITEM_DRAFT, to: 'approved' }])
  })

  it('reopens a cancelled order with the items its cancel took', async () => {
    const { user } = order(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(screen.getByRole('button', { name: 'Cancel order…' }))
    await user.click(screen.getByRole('button', { name: 'Cancel the order' }))
    await waitFor(() => expect(state.orders.find((o) => o.id === ORDER_DRAFT)?.status).toBe('cancelled'))
    expect(state.items.find((i) => i.id === ITEM_DRAFT)?.status).toBe('cancelled')
    await user.click(await screen.findByRole('button', { name: 'Reopen order' }))
    await waitFor(() => expect(state.orders.find((o) => o.id === ORDER_DRAFT)?.status).toBe('draft'))
    expect(state.items.find((i) => i.id === ITEM_DRAFT)?.status).toBe('draft')
    expect(await screen.findByRole('button', { name: 'Accept order' })).toBeInTheDocument()
  })

  it('adds up a draft before anything is accepted', async () => {
    const { user } = order(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(screen.getByTestId('order-unpriced')).toHaveTextContent('Name Keychain')
    await user.click(within(screen.getByTestId(`order-item-${ITEM_DRAFT}`)).getByRole('button', { name: 'Name Keychain' }))
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    await user.type(within(drawer).getByLabelText(/Price per unit/), '12.50')
    await user.click(within(drawer).getByRole('button', { name: 'Record only' }))
    await user.click(within(drawer).getByRole('button', { name: 'Close' }))
    const totals = screen.getByTestId('order-totals')
    await waitFor(() => expect(totals).toHaveTextContent('of which not accepted'))
    // 12.50 a unit, two of them.
    expect(totals).toHaveTextContent('25.00')
    expect(within(screen.getByTestId(`order-item-${ITEM_DRAFT}`)).getByText('not accepted')).toBeInTheDocument()
    expect(screen.queryByTestId('order-unpriced')).not.toBeInTheDocument()
  })
})
