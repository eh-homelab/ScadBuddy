import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { ORDER_DRAFT, OUTPUT_REAGAN } from '../../mocks/features/orders/state'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { AddToOrderDialog } from './AddToOrderDialog'

function Location() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}</output>
}

describe('AddToOrderDialog (#4249)', () => {
  beforeEach(() => resetMockState())

  it("adds the output to the customer's open order and moves to it", async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const onClose = vi.fn()
    const { user } = renderPage(
      <>
        <AddToOrderDialog open outputId={OUTPUT_REAGAN} outputName="Reagan" onClose={onClose} />
        <Location />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'Ada Lovelace')
    await waitFor(() => expect(within(dialog).getByRole('combobox', { name: 'Order' })).toHaveTextContent('ORD-2026-3'))
    await user.clear(within(dialog).getByLabelText('Quantity'))
    await user.type(within(dialog).getByLabelText('Quantity'), '3')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(`/orders/${ORDER_DRAFT}`))
    expect(add).toHaveBeenCalledWith(ORDER_DRAFT, { output_id: OUTPUT_REAGAN, quantity: 3 })
    expect(onClose).toHaveBeenCalled()
    add.mockRestore()
  })

  it('makes a new customer and a new order when asked', async () => {
    const { user } = renderPage(
      <>
        <AddToOrderDialog open outputId={OUTPUT_REAGAN} onClose={() => {}} />
        <Location />
      </>,
    )
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
    const customers = await api.customers({ q: 'grace' })
    expect(customers).toHaveLength(1)
  })
})
