import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { Quote } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, state } from '../../mocks/features/orders/state'
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

/** A typed quote of the draft item, not accepted. */
function manual(id: string, unitMinor: number, createdAt: string): Quote {
  return {
    id,
    order_item_id: ITEM_DRAFT,
    created_at: createdAt,
    source: 'manual',
    accepted: false,
    sliced_sha256: null,
    sliced_blob_key: null,
    plates: [],
    lines: [{ kind: 'manual', label: 'Quoted by hand', quantity: 1, unit_minor: unitMinor, amount_minor: unitMinor }],
    unit_cost_minor: unitMinor,
    unit_price_minor: unitMinor,
    quantity: 2,
    total_minor: unitMinor * 2,
    created_by: { kind: 'browser', principal: null, session: null },
  }
}

const row = (id: string) => screen.getByTestId(`order-item-${id}`)
const quotesOfDraft = () => state.quotes.filter((q) => q.order_item_id === ITEM_DRAFT)

// The items table prices an order: a unit price beside the quantity, typed and accepted in
// place while the order is draft or quoted, and shown only once it is accepted.
describe('OrderItemsTable · unit price', () => {
  beforeEach(() => resetMockState())

  it("shows an accepted order's unit price and line total, with no editor", async () => {
    render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const table = screen.getByRole('table', { name: 'Line items' })
    expect(within(table).getByRole('columnheader', { name: 'Unit price' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'Line total' })).toBeInTheDocument()
    const cells = within(row(ITEM_APPROVED)).getAllByRole('cell')
    // Picture, item, status, qty, unit price, line total: 15.00 a unit, two of them.
    expect(cells[3]).toHaveTextContent('2')
    expect(cells[4]).toHaveTextContent('$15.00')
    expect(cells[5]).toHaveTextContent('$30.00')
    expect(within(row(ITEM_APPROVED)).queryByText('not accepted')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: /^Unit price for/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Accept unit price' })).not.toBeInTheDocument()
  })

  it("shows the newest quote's unit price before one is accepted, by instant", async () => {
    // A whole second sorts after its own fraction as text; the newest is the .123 one.
    state.quotes.push(manual('q-later', 725, '2026-10-24T23:00:00.123Z'), manual('q-whole', 800, '2026-10-24T23:00:00Z'))
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })).toHaveValue('7.25')
    expect(within(row(ITEM_DRAFT)).getByText('not accepted')).toBeInTheDocument()
    expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[5]).toHaveTextContent('$14.50')
  })

  it('records a typed price and accepts it on Enter', async () => {
    const quoted = vi.spyOn(api, 'quoteOrderItem')
    const accepted = vi.spyOn(api, 'acceptOrderQuote')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })
    expect(input).toHaveValue('')
    await user.type(input, '12.50{Enter}')
    await waitFor(() => expect(quotesOfDraft().map((q) => [q.unit_price_minor, q.accepted])).toEqual([[1250, true]]))
    expect(quoted).toHaveBeenCalledWith(ORDER_DRAFT, ITEM_DRAFT, { unit_price_minor: 1250 })
    expect(accepted).toHaveBeenCalledWith(ORDER_DRAFT, ITEM_DRAFT, quotesOfDraft()[0]?.id)
    // The reloaded row: accepted, at 12.50 a unit and 25.00 for two.
    await waitFor(() => expect(within(row(ITEM_DRAFT)).getByText('Approved')).toBeInTheDocument())
    expect(within(row(ITEM_DRAFT)).queryByText('not accepted')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })).toHaveValue('12.50')
    expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[5]).toHaveTextContent('$25.00')
    expect(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' })).toBeDisabled()
    expect(screen.queryByTestId('order-unpriced')).not.toBeInTheDocument()
    quoted.mockRestore()
    accepted.mockRestore()
  })

  it('accepts a pending price with its button', async () => {
    state.quotes.push(manual('q-pending', 900, '2026-10-24T23:00:00Z'))
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })
    await user.clear(input)
    await user.type(input, '10')
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' }))
    await waitFor(() => expect(within(row(ITEM_DRAFT)).queryByText('not accepted')).not.toBeInTheDocument())
    expect(quotesOfDraft().find((q) => q.accepted)?.unit_price_minor).toBe(1000)
    expect(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })).toHaveValue('10.00')
  })

  it('puts the price back on Escape', async () => {
    state.quotes.push(manual('q-pending', 900, '2026-10-24T23:00:00Z'))
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })
    await user.clear(input)
    await user.type(input, '42{Escape}')
    expect(input).toHaveValue('9.00')
    expect(quotesOfDraft()).toHaveLength(1)
  })

  it.each(['12.345', 'abc', '-1'])('refuses %j without recording a quote', async (typed) => {
    const quoted = vi.spyOn(api, 'quoteOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })
    await user.type(input, `${typed}{Enter}`)
    expect(await within(row(ITEM_DRAFT)).findByRole('alert')).toHaveTextContent('Enter a price per unit, such as 12.50.')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Enter a price per unit, such as 12.50.')
    expect(quoted).not.toHaveBeenCalled()
    expect(quotesOfDraft()).toHaveLength(0)
    quoted.mockRestore()
  })

  it("shows the lifecycle's refusal in the row", async () => {
    const order = state.orders.find((o) => o.id === ORDER_DRAFT)
    if (order) order.on_hold = true
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.type(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' }), '12.50{Enter}')
    expect(await within(row(ITEM_DRAFT)).findByRole('alert')).toHaveTextContent(/on hold/)
    // The quote stands, not accepted, as the backend keeps it.
    expect(quotesOfDraft().map((q) => q.accepted)).toEqual([false])
  })

  it('shows a cancelled item’s price without an editor', async () => {
    state.quotes.push(manual('q-pending', 900, '2026-10-24T23:00:00Z'))
    const draft = state.items.find((i) => i.id === ITEM_DRAFT)
    if (draft) draft.status = 'cancelled'
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(screen.queryByRole('textbox', { name: 'Unit price for Name Keychain' })).not.toBeInTheDocument()
    expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[4]).toHaveTextContent('$9.00')
    // In no sum: the line total says "not counted", and the unit price never "not accepted".
    expect(within(row(ITEM_DRAFT)).getByText('not counted')).toBeInTheDocument()
    expect(within(row(ITEM_DRAFT)).queryByText('not accepted')).not.toBeInTheDocument()
  })
})
