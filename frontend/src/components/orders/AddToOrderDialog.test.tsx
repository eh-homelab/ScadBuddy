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
    await user.type(within(dialog).getByLabelText('Email'), 'grace@example.com')
    await user.type(within(dialog).getByLabelText('Phone'), '+44 20 7946 0000')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
    const customers = await api.customers({ q: 'grace' })
    expect(customers).toHaveLength(1)
    expect(customers[0]?.contacts).toEqual([
      expect.objectContaining({ kind: 'email', value: 'grace@example.com', primary: true }),
      expect.objectContaining({ kind: 'phone', value: '+44 20 7946 0000', primary: true }),
    ])
    const order = screen.getByTestId('location').textContent?.split('/').pop() ?? ''
    expect((await api.order(order)).order.customer_id).toBe(customers[0]?.id)
  })

  it('refuses a new customer with no name, and makes nothing (#4251)', async () => {
    const createCustomer = vi.spyOn(api, 'createCustomer')
    const { user } = renderPage(<AddToOrderDialog open outputId={OUTPUT_REAGAN} onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer' }), 'New customer…')
    await user.click(within(dialog).getByRole('button', { name: 'Add to order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Name the new customer.')
    expect(createCustomer).not.toHaveBeenCalled()
    createCustomer.mockRestore()
  })

  it('shows a rough estimate once a filament is picked, and records nothing (#4250)', async () => {
    const add = vi.spyOn(api, 'addOrderItem')
    const { user } = renderPage(<AddToOrderDialog open outputId={OUTPUT_REAGAN} outputName="Reagan" onClose={() => {}} />)
    const dialog = screen.getByRole('dialog', { name: 'Add to order' })
    await choose(user, await within(dialog).findByRole('combobox', { name: 'Estimate with' }), /Sunflower Yellow/)
    const estimate = await within(dialog).findByTestId('order-estimate')
    expect(estimate).toHaveTextContent(/An estimate: about [\d.]+ g/)
    expect(estimate).toHaveTextContent('$')
    expect(add).not.toHaveBeenCalled()
    add.mockRestore()
  })
})
