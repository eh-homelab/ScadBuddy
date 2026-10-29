import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, type ApiError } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, PROBLEMS, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'

function render(id: string) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route: `/orders/${id}` },
  )
}

const failure = (promise: Promise<unknown>) => promise.then(() => undefined, (e: unknown) => e as ApiError)
const customOf = (orderId: string) => state.items.filter((i) => i.order_id === orderId && i.kind === 'custom')

// #4954 — a custom line item: a title, a quantity and a typed price, with nothing behind it.
describe('custom line items', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('adds one from the row under the items on Enter, priced and accepted', async () => {
    const added = vi.spyOn(api, 'addOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    // "Add custom item" moves the focus to the entry row.
    await user.click(screen.getByRole('button', { name: 'Add custom item' }))
    const title = screen.getByRole('textbox', { name: 'Custom item title' })
    expect(title).toHaveFocus()
    await user.type(title, 'Design work')
    await user.type(screen.getByRole('textbox', { name: 'Custom item SKU' }), 'DES-1')
    await user.clear(screen.getByRole('textbox', { name: 'Custom item quantity' }))
    await user.type(screen.getByRole('textbox', { name: 'Custom item quantity' }), '2')
    await user.type(screen.getByRole('textbox', { name: 'Custom item unit price' }), '25{Enter}')
    await waitFor(() => expect(customOf(ORDER_DRAFT)).toHaveLength(1))
    expect(added).toHaveBeenCalledWith(ORDER_DRAFT, {
      kind: 'custom',
      title: 'Design work',
      quantity: 2,
      unit_price_minor: 2500,
      notes: '',
      sku: 'DES-1',
      requires_printing: false,
    })
    const item = customOf(ORDER_DRAFT)[0]!
    // The grid shows it with a Custom badge for a picture, at its accepted price.
    const row = await screen.findByTestId(`order-item-${item.id}`)
    expect(within(row).getByRole('img', { name: 'Design work is a custom item' })).toHaveTextContent('Custom')
    expect(within(row).getByText('SKU DES-1')).toBeInTheDocument()
    expect(within(row).getAllByRole('cell')[5]).toHaveTextContent('$50.00')
    expect(within(row).queryByText('not accepted')).not.toBeInTheDocument()
    // The row clears for the next one.
    expect(screen.getByRole('textbox', { name: 'Custom item title' })).toHaveValue('')
  })

  it('says what the entry row lacks, and sends nothing', async () => {
    const added = vi.spyOn(api, 'addOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.type(screen.getByRole('textbox', { name: 'Custom item unit price' }), '5{Enter}')
    expect(await screen.findByText('Type a title for the custom item.')).toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'Custom item title' }), 'Fee')
    await user.clear(screen.getByRole('textbox', { name: 'Custom item unit price' }))
    await user.type(screen.getByRole('textbox', { name: 'Custom item unit price' }), 'abc{Enter}')
    expect(await screen.findByText('Enter a price per unit, such as 12.50.')).toBeInTheDocument()
    expect(added).not.toHaveBeenCalled()
  })

  it('reprices a custom item from its unit price cell', async () => {
    const item = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Rush fee', quantity: 1, unit_price_minor: 500 })
    const updated = vi.spyOn(api, 'updateOrderItem')
    const quoted = vi.spyOn(api, 'quoteOrderItem')
    const { user } = render(ORDER_DRAFT)
    const price = await screen.findByRole('textbox', { name: 'Unit price for Rush fee' })
    expect(price).toHaveValue('5.00')
    await user.clear(price)
    await user.type(price, '7.5{Enter}')
    await waitFor(() => expect(updated).toHaveBeenCalledWith(ORDER_DRAFT, item.id, { unit_price_minor: 750 }))
    expect(quoted).not.toHaveBeenCalled()
    await waitFor(() => expect(within(screen.getByTestId(`order-item-${item.id}`)).getAllByRole('cell')[5]).toHaveTextContent('$7.50'))
  })

  it('edits a custom item in its drawer, with no slice quote and no inputs', async () => {
    const item = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Packaging', quantity: 1, unit_price_minor: 300 })
    const updated = vi.spyOn(api, 'updateOrderItem')
    const { user } = render(ORDER_DRAFT)
    await user.click(await screen.findByRole('button', { name: 'Packaging' }))
    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getAllByText('Custom item').length).toBeGreaterThan(0)
    expect(within(drawer).queryByText('Inputs')).not.toBeInTheDocument()
    expect(within(drawer).queryByTestId('quote-from-slice')).not.toBeInTheDocument()
    await user.clear(within(drawer).getByLabelText('Quantity'))
    await user.type(within(drawer).getByLabelText('Quantity'), '3')
    await user.clear(within(drawer).getByLabelText('Price per unit (USD)'))
    await user.type(within(drawer).getByLabelText('Price per unit (USD)'), '2')
    await user.click(within(drawer).getByRole('checkbox', { name: 'Requires printing' }))
    await user.click(within(drawer).getByRole('button', { name: 'Save item' }))
    await waitFor(() =>
      expect(updated).toHaveBeenCalledWith(ORDER_DRAFT, item.id, { quantity: 3, unit_price_minor: 200, requires_printing: true }),
    )
    const saved = state.items.find((i) => i.id === item.id)
    expect(saved?.status).toBe('approved')
    expect(state.quotes.filter((q) => q.order_item_id === item.id && q.accepted).map((q) => q.total_minor)).toEqual([600])
  })

  it('marks a custom item that needs no printing done', async () => {
    const order = await api.createOrder({ customer_id: state.orders[0]!.customer_id })
    const item = await api.addOrderItem(order.id, { kind: 'custom', title: 'Design work', quantity: 2, unit_price_minor: 1000 })
    await api.acceptOrder(order.id)
    const { user } = render(order.id)
    await user.click(await screen.findByRole('button', { name: 'Design work' }))
    const drawer = await screen.findByRole('dialog')
    await user.click(within(drawer).getByRole('button', { name: 'Mark done' }))
    await waitFor(() => expect(state.items.find((i) => i.id === item.id)?.status).toBe('fulfilled'))
    expect(state.fulfilments.filter((f) => f.order_item_id === item.id).map((f) => [f.quantity, Boolean(f.accepted_at)])).toEqual([[2, true]])
  })

  it('marks done by accepting the attempt already recorded, never a second one', async () => {
    const order = await api.createOrder({ customer_id: state.orders[0]!.customer_id })
    const item = await api.addOrderItem(order.id, { kind: 'custom', title: 'Design work', quantity: 1, unit_price_minor: 1000 })
    await api.acceptOrder(order.id)
    const open = await api.recordFulfilment(order.id, item.id, { quantity: 1, outcome: 'succeeded' })
    const recorded = vi.spyOn(api, 'recordFulfilment')
    const { user } = render(order.id)
    await user.click(await screen.findByRole('button', { name: 'Design work' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Mark done' }))
    await waitFor(() => expect(state.items.find((i) => i.id === item.id)?.status).toBe('fulfilled'))
    expect(recorded).not.toHaveBeenCalled()
    expect(state.fulfilments.filter((f) => f.order_item_id === item.id).map((f) => f.id)).toEqual([open.id])
  })

  it('leaves a partial attempt recorded by hand for its own Accept', async () => {
    const order = await api.createOrder({ customer_id: state.orders[0]!.customer_id })
    const item = await api.addOrderItem(order.id, { kind: 'custom', title: 'Design work', quantity: 3, unit_price_minor: 1000 })
    await api.acceptOrder(order.id)
    const partial = await api.recordFulfilment(order.id, item.id, { quantity: 1, outcome: 'partial' })
    const { user } = render(order.id)
    await user.click(await screen.findByRole('button', { name: 'Design work' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Mark done' }))
    await waitFor(() => expect(state.items.find((i) => i.id === item.id)?.status).toBe('fulfilled'))
    const attempts = state.fulfilments.filter((f) => f.order_item_id === item.id)
    expect(attempts.map((f) => [f.quantity, f.notes, Boolean(f.accepted_at)])).toEqual([
      [1, '', false],
      [3, 'Marked done', true],
    ])
    expect(attempts[0]!.id).toBe(partial.id)
  })

  it("leaves an agent's price quoted (accept: false), and refuses a typed quote of its own", async () => {
    const item = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Fee', quantity: 2, unit_price_minor: 100, accept: false })
    expect(item.status).toBe('quoted')
    const again = await api.updateOrderItem(ORDER_DRAFT, item.id, { quantity: 3, accept: false })
    expect(again.status).toBe('quoted')
    const newest = state.quotes.filter((q) => q.order_item_id === item.id).at(-1)
    expect([newest?.accepted, newest?.total_minor]).toEqual([false, 300])
    const typed = await failure(api.quoteOrderItem(ORDER_DRAFT, item.id, { unit_price_minor: 1 }))
    expect(typed?.problem.type).toBe(`${PROBLEMS}item_custom`)
  })

  it('answers the shapes the backend refuses', async () => {
    const shape = await failure(api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'X', quantity: 1, unit_price_minor: 1, slug: 'name-keychain' }))
    expect(shape?.status).toBe(422)
    const price = await failure(api.updateOrderItem(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 5 }))
    expect(price?.status).toBe(422)
    const locked = await failure(api.addOrderItem(ORDER_ACCEPTED, { kind: 'custom', title: 'X', quantity: 1, unit_price_minor: 1 }))
    expect(locked?.problem.type).toBe(`${PROBLEMS}item_locked`)
    const custom = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'X', quantity: 1, unit_price_minor: 1 })
    const sliced = await fetch(`/api/v1/orders/${ORDER_DRAFT}/items/${custom.id}/quote`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': 'k1' },
      body: JSON.stringify({ filaments: {} }),
    })
    expect(sliced.status).toBe(409)
    expect(((await sliced.json()) as { type: string }).type).toBe(`${PROBLEMS}item_custom`)
  })
})
