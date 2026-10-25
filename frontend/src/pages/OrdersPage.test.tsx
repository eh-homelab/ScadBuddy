import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
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

  // #4251: the customer made inside the dialog, with the order's notes and due date.
  it('makes a new customer inline, then the order for them', async () => {
    const createOrder = vi.spyOn(api, 'createOrder')
    const { user } = renderPage(
      <>
        <OrdersPage />
        <Location />
      </>,
      { route: '/orders' },
    )
    await screen.findByRole('table', { name: 'Orders' })
    await user.click(screen.getByRole('button', { name: 'New order' }))
    const dialog = screen.getByRole('dialog', { name: 'New order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer for the new order' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.type(within(dialog).getByLabelText('Email'), 'grace@example.com')
    await user.type(within(dialog).getByLabelText('Due'), '2026-12-01')
    await user.type(within(dialog).getByLabelText('Notes'), 'Gift wrap')
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
    const [grace, ...others] = await api.customers({ q: 'grace' })
    expect(others).toHaveLength(0)
    expect(grace?.contacts).toEqual([expect.objectContaining({ kind: 'email', value: 'grace@example.com', primary: true })])
    expect(createOrder).toHaveBeenCalledWith({ customer_id: grace?.id, notes: 'Gift wrap', due_at: '2026-12-01T00:00:00Z' })
    createOrder.mockRestore()
  })

  it('opens New order empty after a cancelled attempt', async () => {
    const { user } = renderPage(<OrdersPage />, { route: '/orders' })
    await screen.findByRole('table', { name: 'Orders' })
    await user.click(screen.getByRole('button', { name: 'New order' }))
    let dialog = screen.getByRole('dialog', { name: 'New order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer for the new order' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.type(within(dialog).getByLabelText('Due'), '2026-12-01')
    await user.type(within(dialog).getByLabelText('Notes'), 'Gift wrap')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'New order' })).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'New order' }))
    dialog = screen.getByRole('dialog', { name: 'New order' })
    expect(within(dialog).queryByLabelText('Customer name')).toBeNull()
    expect(within(dialog).getByLabelText('Due')).toHaveValue('')
    expect(within(dialog).getByLabelText('Notes')).toHaveValue('')
  })

  it('refuses a new customer with no name, and makes nothing', async () => {
    const createCustomer = vi.spyOn(api, 'createCustomer')
    const { user } = renderPage(<OrdersPage />, { route: '/orders' })
    await screen.findByRole('table', { name: 'Orders' })
    await user.click(screen.getByRole('button', { name: 'New order' }))
    const dialog = screen.getByRole('dialog', { name: 'New order' })
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Pick a customer, or make a new one.')
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer for the new order' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), '   ')
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    await waitFor(() => expect(within(dialog).getByRole('alert')).toHaveTextContent('Name the new customer.'))
    expect(createCustomer).not.toHaveBeenCalled()
    createCustomer.mockRestore()
  })

  it('keeps the customer it made when the order fails, so a retry makes no second', async () => {
    const createOrder = vi.spyOn(api, 'createOrder').mockRejectedValueOnce(new Error('The server is away.'))
    const { user } = renderPage(
      <>
        <OrdersPage />
        <Location />
      </>,
      { route: '/orders' },
    )
    await screen.findByRole('table', { name: 'Orders' })
    await user.click(screen.getByRole('button', { name: 'New order' }))
    const dialog = screen.getByRole('dialog', { name: 'New order' })
    const picker = within(dialog).getByRole('combobox', { name: 'Customer for the new order' })
    await choose(user, picker, 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server is away.')
    expect(picker).toHaveTextContent('Grace Hopper')
    expect(within(dialog).queryByLabelText('Customer name')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
    expect(await api.customers({ q: 'grace' })).toHaveLength(1)
    expect(createOrder).toHaveBeenCalledTimes(2)
    createOrder.mockRestore()
  })
})
