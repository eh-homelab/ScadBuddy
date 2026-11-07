import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { resetMockState } from '../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT } from '../mocks/features/orders/state'
import { choose } from '../test/select'
import { renderPage } from '../test/utils'
import type { OrderRow } from '../api/types'
import { sortByMargin } from '../components/orders/marginSort'
import { DEFAULT_ORDERS_QUERY } from '../lib/ordersQuery'
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

  it('refuses a new customer whose email is not an address, and makes nothing (#4691)', async () => {
    const createCustomer = vi.spyOn(api, 'createCustomer')
    const { user } = renderPage(<OrdersPage />, { route: '/orders' })
    await screen.findByRole('table', { name: 'Orders' })
    await user.click(screen.getByRole('button', { name: 'New order' }))
    const dialog = screen.getByRole('dialog', { name: 'New order' })
    await choose(user, within(dialog).getByRole('combobox', { name: 'Customer for the new order' }), 'New customer…')
    await user.type(within(dialog).getByLabelText('Customer name'), 'Grace Hopper')
    await user.type(within(dialog).getByLabelText('Email'), 'grace at example')
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('Enter the email as name@example.com, or leave it empty.')
    expect(createCustomer).not.toHaveBeenCalled()
    await user.clear(within(dialog).getByLabelText('Email'))
    await user.type(within(dialog).getByLabelText('Email'), 'grace@example.com')
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    await waitFor(() => expect(createCustomer).toHaveBeenCalledTimes(1))
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
    const listed = vi.spyOn(api, 'customers')
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server is away.')
    // The list's customers are not re-read under the open dialog (#4728); the picker
    // offers the new one.
    expect(listed).not.toHaveBeenCalled()
    expect(picker).toHaveTextContent('Grace Hopper')
    expect(within(dialog).queryByLabelText('Customer name')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Create order' }))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/orders\/o-/))
    // Closed, the list re-reads them, so its names and filter take the new one in.
    expect(listed).toHaveBeenCalled()
    listed.mockRestore()
    expect(await api.customers({ q: 'grace' })).toHaveLength(1)
    expect(createOrder).toHaveBeenCalledTimes(2)
    createOrder.mockRestore()
  })
})

describe('OrdersPage margin (#5327)', () => {
  beforeEach(() => resetMockState())

  it('shows each margin, tints a loss, and filters to loss or thin', async () => {
    const { user } = renderPage(
      <>
        <OrdersPage />
        <Location />
      </>,
      { route: '/orders' },
    )
    await screen.findByRole('table', { name: 'Orders' })
    const draft = screen.getByTestId(`order-row-${ORDER_DRAFT}`)
    expect(within(draft).getByText('—')).toBeInTheDocument()
    expect((await api.orderCosts(ORDER_ACCEPTED)).margin_state).toBe('ok')

    await api.addCostEntry(ORDER_ACCEPTED, { kind: 'other', amount_minor: 1_000_000 })
    await user.click(screen.getByRole('radio', { name: 'Loss or thin' }))
    expect(screen.getByTestId('location')).toHaveTextContent('/orders?margin=loss_thin')
    await waitFor(() => expect(screen.queryByTestId(`order-row-${ORDER_DRAFT}`)).toBeNull())
    const row = screen.getByTestId(`order-row-${ORDER_ACCEPTED}`)
    expect(row.className).toContain('text-destructive')
    const costs = await api.orderCosts(ORDER_ACCEPTED)
    const margin = within(row).getByTestId('order-margin')
    expect(margin).toHaveTextContent(`${costs.margin_percent}%`)
    expect(margin.getAttribute('title')).toMatch(/^Margin /)
  })

  it('cycles the Margin header through ascending, descending and none, with aria-sort (#5515)', async () => {
    await api.addCostEntry(ORDER_ACCEPTED, { kind: 'other', amount_minor: 1_000_000 })
    const loaded = (await api.orders(DEFAULT_ORDERS_QUERY, { limit: 50 })).items
    const { user } = renderPage(<OrdersPage />, { route: '/orders' })
    const table = await screen.findByRole('table', { name: 'Orders' })
    const header = within(table).getByRole('columnheader', { name: 'Margin' })
    const order = () => within(table).getAllByRole('row').slice(1).map((r) => r.getAttribute('data-testid'))
    const ids = (rows: OrderRow[]) => rows.map((r) => `order-row-${r.id}`)
    const unsorted = order()
    expect(unsorted).toEqual(ids(loaded))
    expect(header).toHaveAttribute('aria-sort', 'none')

    await user.click(within(header).getByRole('button', { name: 'Margin' }))
    expect(header).toHaveAttribute('aria-sort', 'ascending')
    expect(order()).toEqual(ids(sortByMargin(loaded, 'ascending')))
    expect(order()[0]).toBe(`order-row-${ORDER_ACCEPTED}`)
    expect(order().at(-1)).toBe(`order-row-${ORDER_DRAFT}`)

    await user.click(within(header).getByRole('button', { name: 'Margin' }))
    expect(header).toHaveAttribute('aria-sort', 'descending')
    expect(order()).toEqual(ids(sortByMargin(loaded, 'descending')))
    expect(order().at(-1)).toBe(`order-row-${ORDER_DRAFT}`)

    await user.click(within(header).getByRole('button', { name: 'Margin' }))
    expect(header).toHaveAttribute('aria-sort', 'none')
    expect(order()).toEqual(unsorted)
  })

  it('keeps Load more on an empty page that still has a cursor (#5515)', async () => {
    const [row] = (await api.orders(DEFAULT_ORDERS_QUERY, { limit: 1 })).items
    if (!row) throw new Error('the mocks hold no order')
    const list = vi.spyOn(api, 'orders')
    // Two empty pages that each end on a cursor, then a page with the match.
    const pages: Record<string, { items: OrderRow[]; next_cursor: string | null }> = {
      first: { items: [], next_cursor: 'c1' },
      c1: { items: [], next_cursor: 'c2' },
      c2: { items: [row], next_cursor: null },
    }
    list.mockImplementation(async (_query, options) => pages[options?.cursor ?? 'first'] ?? { items: [], next_cursor: null })
    const { user } = renderPage(<OrdersPage />, { route: '/orders?margin=loss_thin' })
    expect(await screen.findByText('No matches yet. Load more to search older orders.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(list.mock.calls.some(([, options]) => options?.cursor === 'c1')).toBe(true))
    expect(await screen.findByRole('button', { name: 'Load more' })).toBeEnabled()
    expect(screen.getByText('No matches yet. Load more to search older orders.')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Load more' }))
    expect(await screen.findByTestId(`order-row-${row.id}`)).toBeInTheDocument()
    expect(list.mock.calls.some(([, options]) => options?.cursor === 'c2')).toBe(true)
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
    list.mockRestore()
  })

  it('sorts by margin, worst first, with no margin last', () => {
    const row = (id: string, margin_minor: number | null, margin_percent: number | null) => ({ id, margin_minor, margin_percent }) as OrderRow
    const rows = [row('none', null, null), row('ok', 500, 50), row('zero', -10, null), row('thin', 50, 5)]
    expect(sortByMargin(rows, 'ascending').map((r) => r.id)).toEqual(['zero', 'thin', 'ok', 'none'])
    expect(sortByMargin(rows, 'descending').map((r) => r.id)).toEqual(['ok', 'thin', 'zero', 'none'])
    expect(sortByMargin(rows, 'none')).toBe(rows)
  })
})
