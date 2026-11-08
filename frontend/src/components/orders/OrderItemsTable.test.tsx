import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../../api/client'
import type { Quote } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { seedActivity } from '../../mocks/features/activity'
import { seedOperation } from '../../mocks/features/operations'
import { ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, PROBLEMS, seedCart, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'
import { LOCKED_TEXT, REVISED_TEXT } from './itemActions'
import { QUANTITY_HINT } from './quantity'

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
  afterEach(() => vi.restoreAllMocks())

  it("shows an accepted order's unit price and line total, with no editor", async () => {
    render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const table = screen.getByRole('table', { name: 'Line items' })
    expect(within(table).getByRole('columnheader', { name: 'Unit price' })).toBeInTheDocument()
    expect(within(table).getByRole('columnheader', { name: 'Line total' })).toBeInTheDocument()
    const cells = within(row(ITEM_APPROVED)).getAllByRole('cell')
    // Select, picture, item, status, qty, unit price, line total: 15.00 a unit, two of them.
    expect(cells[4]).toHaveTextContent('2')
    expect(cells[5]).toHaveTextContent('$15.00')
    expect(cells[6]).toHaveTextContent('$30.00')
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
    expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[6]).toHaveTextContent('$14.50')
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
    expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[6]).toHaveTextContent('$25.00')
    expect(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' })).toBeDisabled()
    expect(screen.queryByTestId('order-unpriced')).not.toBeInTheDocument()
  })

  it('keeps Accept off for the accepted price however it is typed', async () => {
    state.quotes.push({ ...manual('q-done', 1250, '2026-10-24T23:00:00Z'), accepted: true })
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })
    const accept = within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' })
    expect(accept).toBeDisabled()
    await user.clear(input)
    await user.type(input, '12.5')
    expect(accept).toBeDisabled()
    await user.type(input, '1')
    expect(accept).toBeEnabled()
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
  })

  it("shows the lifecycle's refusal in the row", async () => {
    const order = state.orders.find((o) => o.id === ORDER_DRAFT)
    if (order) order.on_hold = true
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.type(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' }), '12.50{Enter}')
    expect(await within(row(ITEM_DRAFT)).findByRole('alert')).toHaveTextContent(/on hold/)
    // The quote stands, not accepted, as the backend keeps it, and the row reloads to show
    // it: 12.50 a unit, 25.00 for two, with the refusal still there.
    expect(quotesOfDraft().map((q) => q.accepted)).toEqual([false])
    await waitFor(() => expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[6]).toHaveTextContent('$25.00'))
    expect(within(row(ITEM_DRAFT)).getByText('not accepted')).toBeInTheDocument()
    expect(within(row(ITEM_DRAFT)).getByRole('alert')).toHaveTextContent(/on hold/)
  })

  it("accepts a pending slice quote at its own price, recording nothing", async () => {
    const slice: Quote = { ...manual('q-slice', 900, '2026-10-24T23:00:00Z'), source: 'slice', sliced_sha256: 'a'.repeat(64), sliced_blob_key: 'quote-x', unit_cost_minor: 400 }
    state.quotes.push(slice)
    const quoted = vi.spyOn(api, 'quoteOrderItem')
    const accepted = vi.spyOn(api, 'acceptOrderQuote')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })).toHaveValue('9.00')
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' }))
    await waitFor(() => expect(quotesOfDraft().map((q) => [q.id, q.accepted])).toEqual([['q-slice', true]]))
    expect(accepted).toHaveBeenCalledWith(ORDER_DRAFT, ITEM_DRAFT, 'q-slice')
    expect(quoted).not.toHaveBeenCalled()
  })

  it('retries a refused accept on the quote it recorded, never a second one', async () => {
    const order = state.orders.find((o) => o.id === ORDER_DRAFT)
    if (order) order.on_hold = true
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.type(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' }), '12.50{Enter}')
    expect(await within(row(ITEM_DRAFT)).findByRole('alert')).toHaveTextContent(/on hold/)
    await waitFor(() => expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[6]).toHaveTextContent('$25.00'))
    if (order) order.on_hold = false
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' }))
    await waitFor(() => expect(quotesOfDraft().map((q) => [q.unit_price_minor, q.accepted])).toEqual([[1250, true]]))
  })

  it('records a fresh quote when the pending one is for an old quantity', async () => {
    // Priced for two; the item is now three, so that quote would be `quote_stale`.
    state.quotes.push(manual('q-old', 900, '2026-10-24T23:00:00Z'))
    const draft = state.items.find((i) => i.id === ITEM_DRAFT)
    if (draft) draft.quantity = 3
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })).toHaveValue('9.00')
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' }))
    await waitFor(() => expect(quotesOfDraft().find((q) => q.accepted)?.quantity).toBe(3))
    const accepted = quotesOfDraft().find((q) => q.accepted)
    expect(accepted?.id).not.toBe('q-old')
    expect(accepted?.unit_price_minor).toBe(900)
    expect(within(row(ITEM_DRAFT)).queryByRole('alert')).not.toBeInTheDocument()
  })

  it('keeps the typed text and the refusal, and reloads nothing, when the quote itself is refused (#4726)', async () => {
    const quoted = vi.spyOn(api, 'quoteOrderItem').mockRejectedValueOnce(new Error('The server is away.'))
    const accepted = vi.spyOn(api, 'acceptOrderQuote')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const reload = vi.spyOn(api, 'order')
    const input = screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })
    await user.type(input, '12.50{Enter}')
    expect(await within(row(ITEM_DRAFT)).findByRole('alert')).toHaveTextContent('The server is away.')
    expect(quoted).toHaveBeenCalledTimes(1)
    expect(accepted).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
    expect(input).toHaveValue('12.50')
    expect(quotesOfDraft()).toHaveLength(0)
  })

  it('offers Accept again when the accepted price is for an old quantity (#4726)', async () => {
    // Accepted for two; the item is now three, so the accepted quote no longer prices it.
    state.quotes.push({ ...manual('q-done', 1250, '2026-10-24T23:00:00Z'), accepted: true })
    const draft = state.items.find((i) => i.id === ITEM_DRAFT)
    if (draft) draft.quantity = 3
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })).toHaveValue('12.50')
    expect(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Accept unit price' })).toBeEnabled()
  })

  it('shows a slice quote still running after a reload, and offers no second (#4736)', async () => {
    const at = new Date().toISOString()
    const subject = `order_item:${ITEM_DRAFT}`
    seedOperation({ id: 'op-running', kind: 'order_item_quote', subject, status: 'running', created_at: at })
    seedActivity([
      {
        id: 'operation:op-running',
        kind: 'operation.order_item_quote',
        principal: 'browser',
        session: null,
        via_tool: null,
        subject,
        status: 'running',
        title: `Order item quote ${subject}`,
        parent_id: null,
        detail: {},
        created_at: at,
        updated_at: at,
        finished_at: null,
      },
    ])
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(await within(row(ITEM_DRAFT)).findByText('Quoting…')).toBeInTheDocument()
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Name Keychain' }))
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    expect(within(drawer).getByTestId('quote-from-slice')).toBeDisabled()
  })

  it('clears a picked-up quote once it lands, reloads once, and never follows it again (#4736 review)', async () => {
    const at = new Date().toISOString()
    const subject = `order_item:${ITEM_DRAFT}`
    seedOperation({ id: 'op-landing', kind: 'order_item_quote', subject, status: 'running', created_at: at })
    // The activity row stays `running` after the operation ends, as a swallowed write leaves it.
    seedActivity([
      { id: 'operation:op-landing', kind: 'operation.order_item_quote', principal: 'browser', session: null, via_tool: null, subject, status: 'running', title: `Order item quote ${subject}`, parent_id: null, detail: {}, created_at: at, updated_at: at, finished_at: null },
    ])
    const followed = vi.spyOn(api, 'followQuote')
    const running = vi.spyOn(api, 'runningQuotes')
    const reads = vi.spyOn(api, 'order')
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(await within(row(ITEM_DRAFT)).findByText('Quoting…')).toBeInTheDocument()
    // #5599 — the page reads the order at mount and again on its realtime subscription's
    // resync (`useAsync`, the socket not yet open). Each read of the items looks for
    // running quotes, so the looks before the landing are one or two, as the two reads'
    // answers render apart or together. Count from once both have landed.
    await waitFor(() => expect(reads).toHaveBeenCalledTimes(2))
    await act(async () => {
      await Promise.allSettled(reads.mock.results.map((r) => r.value))
    })
    const looks = running.mock.calls.length
    reads.mockClear()
    const reload = reads
    const landed = manual('q-landed', 900, at)
    state.quotes.push({ ...landed, source: 'slice' })
    seedOperation({ id: 'op-landing', kind: 'order_item_quote', subject, status: 'succeeded', created_at: at, finished_at: at, result: { ...landed } })
    await waitFor(() => expect(within(row(ITEM_DRAFT)).queryByText('Quoting…')).not.toBeInTheDocument(), { timeout: 5000 })
    // The reload it brought looks again, lists the stale row, and does not follow it.
    await waitFor(() => expect(running).toHaveBeenCalledTimes(looks + 1))
    expect(reload).toHaveBeenCalledTimes(1)
    expect(followed).toHaveBeenCalledTimes(1)
  })

  it('shows a cancelled item’s price without an editor', async () => {
    state.quotes.push(manual('q-pending', 900, '2026-10-24T23:00:00Z'))
    const draft = state.items.find((i) => i.id === ITEM_DRAFT)
    if (draft) draft.status = 'cancelled'
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(screen.queryByRole('textbox', { name: 'Unit price for Name Keychain' })).not.toBeInTheDocument()
    expect(within(row(ITEM_DRAFT)).getAllByRole('cell')[5]).toHaveTextContent('$9.00')
    // In no sum: the line total says "not counted", and the unit price never "not accepted".
    expect(within(row(ITEM_DRAFT)).getByText('not counted')).toBeInTheDocument()
    expect(within(row(ITEM_DRAFT)).queryByText('not accepted')).not.toBeInTheDocument()
  })
})

// #5554 — the row and the drawer say when each quote was made and when a slice ran. The
// cases run in New York (EDT in October): 13:04Z reads 09:04 in a 12- or 24-hour locale.
describe('OrderItemsTable · times (#5554)', () => {
  const zone = process.env.TZ
  beforeEach(() => {
    resetMockState()
    process.env.TZ = 'America/New_York'
  })
  afterEach(() => {
    vi.restoreAllMocks()
    if (zone === undefined) delete process.env.TZ
    else process.env.TZ = zone
  })

  it('names the newest quote and the accepted one, with their times', async () => {
    state.quotes.push({ ...manual('q-accepted', 900, '2026-10-29T12:00:00Z'), accepted: true })
    state.quotes.push(manual('q-newer', 950, '2026-10-30T13:04:00Z'))
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    expect(within(row(ITEM_DRAFT)).getByTestId(`item-quoted-${ITEM_DRAFT}`)).toHaveTextContent(/Newest quote Oct 30, 09:04/)
    expect(within(row(ITEM_DRAFT)).getByTestId(`item-accepted-${ITEM_DRAFT}`)).toHaveTextContent(/Accepted quote Oct 29, 08:00/)
  })

  it('shows a slice quote that failed over MCP, read from the activity index, with its times and error', async () => {
    const subject = `order_item:${ITEM_DRAFT}`
    const created = '2026-10-30T13:04:00Z'
    const finished = '2026-10-30T13:06:00Z'
    seedOperation({
      id: 'op-failed',
      kind: 'order_item_quote',
      subject,
      status: 'failed',
      created_at: created,
      finished_at: finished,
      error: { type: 'about:blank', title: 'Unprocessable', status: 422, detail: 'No printer takes this plate.' },
    })
    seedActivity([
      { id: 'operation:op-failed', kind: 'operation.order_item_quote', principal: 'mcp', session: null, via_tool: 'order_item_quote', subject, status: 'failed', title: `Order item quote ${subject}`, parent_id: null, detail: {}, created_at: created, updated_at: finished, finished_at: finished },
    ])
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const failed = await within(row(ITEM_DRAFT)).findByTestId(`item-quote-failed-${ITEM_DRAFT}`)
    expect(failed).toHaveTextContent(/Quote failed Oct 30, 09:06/)
    expect(failed).toHaveAttribute('title', expect.stringContaining('No printer takes this plate.'))
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Name Keychain' }))
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    expect(within(drawer).getByTestId('quote-error')).toHaveTextContent('No printer takes this plate.')
    expect(within(drawer).getByTestId('quote-error-times')).toHaveTextContent(/Started Oct 30, 09:04.*ended Oct 30, 09:06/)
  })
})

// #4952 review: a fulfilled row's menu offers Unfulfil, disabled with the backend's reason.
describe('OrderItemsTable · row menu', () => {
  beforeEach(() => {
    resetMockState()
    state.fulfilments.push({
      id: 'f-made',
      order_item_id: ITEM_APPROVED,
      purpose: 'production',
      attempt: 1,
      quantity: 2,
      outcome: 'succeeded',
      failure_reason: null,
      started_at: null,
      finished_at: null,
      notes: '',
      accepted_at: '2026-10-20T10:00:00Z',
      created_by: { kind: 'browser', principal: null, session: null },
      created_at: '2026-10-20T09:00:00Z',
    })
    state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'fulfilled'
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'ready'
  })

  async function openMenu() {
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row(ITEM_APPROVED)).getByRole('button', { name: 'Row actions' }))
    return screen.findByRole('menuitem', { name: 'Unfulfil…' })
  }

  it('disables Unfulfil on an order on hold, and says why', async () => {
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.on_hold = true
    expect(await openMenu()).toHaveAttribute('data-disabled')
    // Unfulfil's, and Duplicate's (#4955): a copy is an add after acceptance, refused on hold.
    expect(screen.getAllByText('The order is on hold. Release it first.')).toHaveLength(2)
  })

  it('disables Unfulfil for an item a shipment carries, and says why', async () => {
    state.shipments.push({
      id: 's-made',
      order_id: ORDER_ACCEPTED,
      carrier: 'Post',
      service: null,
      tracking: null,
      shipped_at: null,
      address: null,
      cost_minor: null,
      notes: '',
      created_at: '2026-10-21T09:00:00Z',
      items: [{ order_item_id: ITEM_APPROVED, quantity: 2 }],
    })
    expect(await openMenu()).toHaveAttribute('data-disabled')
    expect(screen.getByText('A shipment carries this item. Take it off the shipment first.')).toBeInTheDocument()
  })

  it('offers Unfulfil, not disabled, when nothing is in the way', async () => {
    expect(await openMenu()).not.toHaveAttribute('data-disabled')
  })
})

// #4955, spec 2026-11-07 §4.1 — one card: its header adds items, a row names its source, and
// every row's ⋯ menu holds the actions allowed now.
describe('OrderItemsTable · the items card (#4955)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('adds items from the card header', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const card = screen.getByRole('region', { name: 'Items' })
    await user.click(within(card).getByRole('button', { name: 'Add product' }))
    expect(await screen.findByRole('dialog', { name: 'Add item' })).toBeInTheDocument()
  })

  it('focuses the custom item row from the card header', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(screen.getByRole('region', { name: 'Items' })).getByRole('button', { name: 'Add custom item' }))
    expect(screen.getByRole('textbox', { name: 'Custom item title' })).toHaveFocus()
  })

  it("shows a row's loss badge from the money view, and pads the custom item row to match", async () => {
    const real = await api.orderCosts(ORDER_DRAFT)
    const base = real.items[0] ?? { item_id: ITEM_DRAFT }
    vi.spyOn(api, 'orderCosts').mockResolvedValue({ ...real, items: [{ ...base, item_id: ITEM_DRAFT, margin_state: 'loss' }] } as typeof real)
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await waitFor(() => expect(within(row(ITEM_DRAFT)).getByText('Loss')).toBeInTheDocument())
    const body = within(row(ITEM_DRAFT)).getAllByRole('cell').length
    expect(within(screen.getByTestId('custom-item-row')).getAllByRole('cell')).toHaveLength(body)
  })

  it('names where each item came from', async () => {
    const added = await api.addOrderItem(ORDER_DRAFT, { library_file_id: 120, quantity: 1, title: 'Drawer label' })
    render(ORDER_DRAFT)
    expect(await screen.findByTestId(`item-source-${added.id}`)).toHaveTextContent('file')
    expect(screen.getByTestId(`item-source-${ITEM_DRAFT}`)).toHaveTextContent(/^(model|output)$/)
  })

  it("offers a draft item's actions, and opens the drawer at its quote", async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
    const names = (await screen.findAllByRole('menuitem')).map((m) => m.textContent)
    expect(names).toEqual(['Open details', 'Quote from a slice…', 'Duplicate', 'Delete item'])
    await user.click(screen.getByRole('menuitem', { name: 'Quote from a slice…' }))
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    expect(within(drawer).getByTestId('item-section-quote')).toBeInTheDocument()
  })

  describe('the section a menu action opens the drawer at', () => {
    const scrolled: string[] = []
    const original = Element.prototype.scrollIntoView
    beforeEach(() => {
      scrolled.length = 0
      // jsdom has no scrollIntoView: record which element each call scrolls.
      Element.prototype.scrollIntoView = function (this: Element) {
        scrolled.push(this.id)
      }
    })
    afterEach(() => {
      Element.prototype.scrollIntoView = original
    })

    it('scrolls the drawer to the quote, and not for Open details', async () => {
      const { user } = render(ORDER_DRAFT)
      await screen.findByRole('heading', { name: 'ORD-2026-3' })
      await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
      await user.click(await screen.findByRole('menuitem', { name: 'Quote from a slice…' }))
      await screen.findByRole('dialog', { name: 'Name Keychain' })
      await waitFor(() => expect(scrolled).toContain('item-section-quote'))
      await user.keyboard('{Escape}')
      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Name Keychain' })).toBeNull())
      scrolled.length = 0
      await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
      await user.click(await screen.findByRole('menuitem', { name: 'Open details' }))
      await screen.findByRole('dialog', { name: 'Name Keychain' })
      expect(scrolled.filter((id) => id.startsWith('item-section-'))).toEqual([])
    })

    it('scrolls again to a second section of the item already open', async () => {
      state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'in_production'
      const { user } = render(ORDER_ACCEPTED)
      await screen.findByRole('heading', { name: 'ORD-2026-2' })
      await user.click(within(row(ITEM_APPROVED)).getByRole('button', { name: 'Row actions' }))
      await user.click(await screen.findByRole('menuitem', { name: /^Print/ }))
      await screen.findByRole('dialog', { name: 'Name Keychain' })
      await waitFor(() => expect(scrolled).toContain('item-section-print'))
      scrolled.length = 0
      // Beside the assistant the drawer is not modal (#798): the row's menu stays reachable.
      const trigger = within(row(ITEM_APPROVED)).getByRole('button', { name: 'Row actions', hidden: true })
      fireEvent.keyDown(trigger, { key: 'Enter' })
      fireEvent.click(await screen.findByRole('menuitem', { name: 'Fulfil…', hidden: true }))
      await waitFor(() => expect(scrolled).toContain('item-section-fulfil'))
    })

    it('does not carry the section to the next item opened from its title', async () => {
      const other = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Rush fee', quantity: 1, unit_price_minor: 500 })
      const { user } = render(ORDER_DRAFT)
      await screen.findByRole('heading', { name: 'ORD-2026-3' })
      await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
      await user.click(await screen.findByRole('menuitem', { name: 'Quote from a slice…' }))
      await screen.findByRole('dialog', { name: 'Name Keychain' })
      await waitFor(() => expect(scrolled).toContain('item-section-quote'))
      scrolled.length = 0
      // Beside the assistant the drawer is not modal (#798): the next row's title is clickable.
      fireEvent.click(within(row(other.id)).getByRole('button', { name: 'Rush fee', hidden: true }))
      await screen.findByRole('dialog', { name: 'Rush fee' })
      expect(scrolled.filter((id) => id.startsWith('item-section-'))).toEqual([])
    })
  })

  it("offers an accepted item's print, fulfil and duplicate, all refused with the reason on hold", async () => {
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.on_hold = true
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row(ITEM_APPROVED)).getByRole('button', { name: 'Row actions' }))
    expect(await screen.findByRole('menuitem', { name: 'Fulfil…' })).toHaveAttribute('data-disabled')
    expect(screen.getByRole('menuitem', { name: /^Print/ })).toHaveAttribute('data-disabled')
    expect(screen.getAllByText('The order is on hold. Release it first.')).toHaveLength(3)
  })

  it('turns the header and menu writes off while a write runs', async () => {
    let finish: () => void = () => undefined
    vi.spyOn(api, 'deleteOrderItem').mockImplementation(() => new Promise<void>((resolve) => (finish = resolve)))
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete item' }))
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Delete Name Keychain?' })).getByRole('button', { name: 'Delete item' }))
    const card = screen.getByRole('region', { name: 'Items' })
    await waitFor(() => expect(within(card).getByRole('button', { name: 'Add product' })).toBeDisabled())
    expect(within(card).getByRole('button', { name: 'Add custom item' })).toBeDisabled()
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
    expect(await screen.findByRole('menuitem', { name: 'Delete item' })).toHaveAttribute('data-disabled')
    await user.keyboard('{Escape}')
    await act(async () => finish())
    await waitFor(() => expect(within(card).getByRole('button', { name: 'Add product' })).toBeEnabled())
  })

  it('deletes a draft item from its menu after a confirm, and keeps it on Keep', async () => {
    const deleted = vi.spyOn(api, 'deleteOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete item' }))
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Delete Name Keychain?' })).getByRole('button', { name: 'Keep' }))
    expect(deleted).not.toHaveBeenCalled()
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Delete item' }))
    await user.click(within(await screen.findByRole('alertdialog', { name: 'Delete Name Keychain?' })).getByRole('button', { name: 'Delete item' }))
    await waitFor(() => expect(screen.queryByTestId(`order-item-${ITEM_DRAFT}`)).not.toBeInTheDocument())
  })

  it('cancels a quoted item from its menu after a confirm', async () => {
    const cancelled = vi.spyOn(api, 'cancelOrderItem')
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row(ITEM_APPROVED)).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Cancel item' }))
    const confirm = await screen.findByRole('alertdialog', { name: /^Cancel .*\?$/ })
    await user.click(within(confirm).getByRole('button', { name: 'Cancel item' }))
    await waitFor(() => expect(cancelled).toHaveBeenCalledWith(ORDER_ACCEPTED, ITEM_APPROVED))
  })
})

// #4955 step 2, spec 2026-11-07 §4.1 — inline edit of quantity, price and title: Enter saves,
// Escape cancels, Tab moves along the row. Past `quoted` the quantity and price are read-only
// with the reason shown; the title stays editable while the item is open.
describe('OrderItemsTable · inline edit (#4955)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())
  const draftItem = () => state.items.find((i) => i.id === ITEM_DRAFT)!

  it('saves a typed quantity on Enter', async () => {
    const updated = vi.spyOn(api, 'updateOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Quantity for Name Keychain' })
    expect(input).toHaveValue('2')
    await user.clear(input)
    await user.type(input, '5{Enter}')
    await waitFor(() => expect(draftItem().quantity).toBe(5))
    expect(updated).toHaveBeenCalledWith(ORDER_DRAFT, ITEM_DRAFT, { quantity: 5 })
    await waitFor(() => expect(screen.getByRole('textbox', { name: 'Quantity for Name Keychain' })).toHaveValue('5'))
  })

  it('puts the quantity back on Escape, and refuses one that is not a whole number', async () => {
    const updated = vi.spyOn(api, 'updateOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Quantity for Name Keychain' })
    await user.clear(input)
    await user.type(input, '2.5{Enter}')
    expect(within(row(ITEM_DRAFT)).getByRole('alert')).toHaveTextContent(QUANTITY_HINT)
    await user.keyboard('{Escape}')
    expect(input).toHaveValue('2')
    expect(within(row(ITEM_DRAFT)).queryByRole('alert')).toBeNull()
    expect(updated).not.toHaveBeenCalled()
  })

  it('moves from the quantity to the unit price with Tab', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(screen.getByRole('textbox', { name: 'Quantity for Name Keychain' }))
    await user.tab()
    expect(screen.getByRole('textbox', { name: 'Unit price for Name Keychain' })).toHaveFocus()
  })

  it('renames an item on Enter, and keeps its title on Escape', async () => {
    const updated = vi.spyOn(api, 'updateOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Edit title' }))
    const title = screen.getByRole('textbox', { name: 'Title for Name Keychain' })
    expect(title).toHaveFocus()
    await user.clear(title)
    await user.type(title, 'Ignored{Escape}')
    expect(screen.queryByRole('textbox', { name: /^Title for/ })).toBeNull()
    expect(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Name Keychain' })).toBeInTheDocument()
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Edit title' }))
    const again = screen.getByRole('textbox', { name: 'Title for Name Keychain' })
    await user.clear(again)
    await user.type(again, 'Tag for Sam{Enter}')
    await waitFor(() => expect(draftItem().title).toBe('Tag for Sam'))
    expect(updated).toHaveBeenCalledWith(ORDER_DRAFT, ITEM_DRAFT, { title: 'Tag for Sam' })
    expect(await within(row(ITEM_DRAFT)).findByRole('button', { name: 'Tag for Sam' })).toBeInTheDocument()
  })

  it("locks an accepted item's quantity and price with the reason, and keeps its title editable", async () => {
    render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    expect(screen.queryByRole('textbox', { name: /^Quantity for/ })).toBeNull()
    expect(screen.queryByRole('textbox', { name: /^Unit price for/ })).toBeNull()
    expect(screen.getByTestId(`item-locked-${ITEM_APPROVED}`)).toHaveTextContent(LOCKED_TEXT)
    expect(within(row(ITEM_APPROVED)).getByRole('button', { name: 'Edit title' })).toBeInTheDocument()
  })

  it('re-quotes an item revised after acceptance, its quantity still locked (#5350)', async () => {
    state.items.find((i) => i.id === ITEM_APPROVED)!.revised_after_acceptance = true
    render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    expect(screen.queryByRole('textbox', { name: /^Quantity for/ })).toBeNull()
    expect(within(row(ITEM_APPROVED)).getByRole('textbox', { name: /^Unit price for/ })).toBeInTheDocument()
    expect(screen.getByTestId(`item-locked-${ITEM_APPROVED}`)).toHaveTextContent(REVISED_TEXT)
  })

  it("prices a revised item's new design from empty, even at the old price, and says it is not accepted (#5350)", async () => {
    state.items.find((i) => i.id === ITEM_APPROVED)!.revised_after_acceptance = true
    const quoted = vi.spyOn(api, 'quoteOrderItem')
    const accepted = vi.spyOn(api, 'acceptOrderQuote')
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const input = within(row(ITEM_APPROVED)).getByRole('textbox', { name: /^Unit price for/ })
    expect(input).toHaveValue('')
    expect(within(row(ITEM_APPROVED)).getByText('not accepted')).toBeInTheDocument()
    await user.type(input, '15.00{Enter}')
    await waitFor(() => expect(accepted).toHaveBeenCalled())
    expect(quoted).toHaveBeenCalledWith(ORDER_ACCEPTED, ITEM_APPROVED, { unit_price_minor: 1500 })
  })

  it("accepts a revised item's pending quote of the new design at its own price, recording nothing (#5350)", async () => {
    const found = state.items.find((i) => i.id === ITEM_APPROVED)!
    found.revised_after_acceptance = true
    const old = state.quotes.find((q) => q.order_item_id === ITEM_APPROVED && q.accepted)!
    state.quotes.push({ ...old, id: 'q-new-design', accepted: false, unit_price_minor: 1500, revision_id: found.current_revision_id, created_at: '2026-11-08T10:00:00Z' })
    const quoted = vi.spyOn(api, 'quoteOrderItem')
    const accepted = vi.spyOn(api, 'acceptOrderQuote')
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const input = within(row(ITEM_APPROVED)).getByRole('textbox', { name: /^Unit price for/ })
    expect(input).toHaveValue('15.00')
    await user.click(within(row(ITEM_APPROVED)).getByRole('button', { name: 'Accept unit price' }))
    await waitFor(() => expect(accepted).toHaveBeenCalledWith(ORDER_ACCEPTED, ITEM_APPROVED, 'q-new-design'))
    expect(quoted).not.toHaveBeenCalled()
  })

  it('keeps the quantity focused after Enter, so Tab goes on from it', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const input = screen.getByRole('textbox', { name: 'Quantity for Name Keychain' })
    await user.clear(input)
    await user.type(input, '4{Enter}')
    await waitFor(() => expect(draftItem().quantity).toBe(4))
    expect(screen.getByRole('textbox', { name: 'Quantity for Name Keychain' })).toHaveFocus()
  })

  it('waits for a running page write before it saves a quantity', async () => {
    let finish: () => void = () => undefined
    vi.spyOn(api, 'cancelOrderItem').mockImplementation(() => new Promise((resolve) => (finish = () => resolve({} as never))))
    const custom = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Rush fee', quantity: 1, unit_price_minor: 500 })
    const updated = vi.spyOn(api, 'updateOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(custom.id)).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Cancel item' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel item' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add product' })).toBeDisabled())
    const input = screen.getByRole('textbox', { name: 'Quantity for Name Keychain' })
    await user.clear(input)
    await user.type(input, '6{Enter}')
    expect(updated).not.toHaveBeenCalled()
    await act(async () => finish())
  })
})

// #4955 step 3, spec 2026-11-07 §4.1 — Duplicate in the ⋯ menu, and bulk actions: select rows,
// then Fulfil (one dialog, a quantity per item), Print or Remove.
describe('OrderItemsTable · duplicate and bulk actions (#4955)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())
  const rowsTitled = (title: string) => screen.getAllByRole('button', { name: title })

  it('duplicates an item from its menu', async () => {
    const duplicated = vi.spyOn(api, 'duplicateOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Duplicate' }))
    await waitFor(() => expect(rowsTitled('Name Keychain')).toHaveLength(2))
    expect(duplicated).toHaveBeenCalledWith(ORDER_DRAFT, ITEM_DRAFT, {})
  })

  it('asks before duplicating on an accepted order, then adds it after acceptance', async () => {
    const duplicated = vi.spyOn(api, 'duplicateOrderItem')
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row(ITEM_APPROVED)).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Duplicate' }))
    const confirm = await screen.findByRole('alertdialog', { name: /^Duplicate/ })
    expect(confirm).toHaveTextContent('ORD-2026-2 is accepted. Adding an item changes what the customer gets.')
    expect(duplicated).not.toHaveBeenCalled()
    await user.click(within(confirm).getByRole('button', { name: 'Duplicate' }))
    await waitFor(() => expect(duplicated).toHaveBeenCalledWith(ORDER_ACCEPTED, ITEM_APPROVED, { after_acceptance: true }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).toBeNull())
  })

  it('selects rows one by one or all at once, and says how many', async () => {
    const custom = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Rush fee', quantity: 1, unit_price_minor: 500 })
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const card = screen.getByRole('region', { name: 'Items' })
    expect(within(card).queryByTestId('bulk-actions')).toBeNull()
    await user.click(within(row(custom.id)).getByRole('checkbox', { name: 'Select Rush fee' }))
    expect(within(card).getByTestId('bulk-actions')).toHaveTextContent('1 selected')
    await user.click(within(card).getByRole('checkbox', { name: 'Select all items' }))
    expect(within(card).getByTestId('bulk-actions')).toHaveTextContent('2 selected')
    await user.click(within(card).getByRole('button', { name: 'Clear selection' }))
    expect(within(card).queryByTestId('bulk-actions')).toBeNull()
  })

  it('fulfils the selected items from one dialog, a quantity for each', async () => {
    const fulfilled = vi.spyOn(api, 'fulfilOrderItems')
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row(ITEM_APPROVED)).getByRole('checkbox', { name: /^Select / }))
    await user.click(within(screen.getByTestId('bulk-actions')).getByRole('button', { name: 'Fulfil…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Fulfil items' })
    const quantity = within(dialog).getByRole('textbox', { name: /^Quantity made of / })
    expect(quantity).toHaveValue('2')
    await user.clear(quantity)
    await user.type(quantity, '1')
    await user.click(within(dialog).getByRole('button', { name: 'Fulfil' }))
    await waitFor(() => expect(fulfilled).toHaveBeenCalledWith(ORDER_ACCEPTED, [{ item_id: ITEM_APPROVED, quantity: 1 }]))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Fulfil items' })).toBeNull())
    // One of two made: the item stays open, and the selection clears.
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('approved')
    expect(screen.queryByTestId('bulk-actions')).toBeNull()
  })

  it("shows the bulk fulfil's refusal in its dialog, by the item's title", async () => {
    vi.spyOn(api, 'fulfilOrderItems').mockRejectedValue(
      new ApiError({ type: `${PROBLEMS}item_not_approved`, title: 'Conflict', status: 409, detail: `item ${ITEM_APPROVED} is printed, not approved`, items: [ITEM_APPROVED] }),
    )
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const title = state.items.find((i) => i.id === ITEM_APPROVED)!.title
    await user.click(within(row(ITEM_APPROVED)).getByRole('checkbox', { name: `Select ${title}` }))
    await user.click(within(screen.getByTestId('bulk-actions')).getByRole('button', { name: 'Fulfil…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Fulfil items' })
    await user.click(within(dialog).getByRole('button', { name: 'Fulfil' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(`(${title})`)
  })

  it('fulfils only the selected items the bulk route takes: approved or failed', async () => {
    const added = await api.addOrderItemAfterAcceptance(ORDER_ACCEPTED, { kind: 'custom', title: 'Rush fee', quantity: 1, unit_price_minor: 500 })
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await screen.findByTestId(`order-item-${added.id}`)
    await user.click(screen.getByRole('checkbox', { name: 'Select all items' }))
    await user.click(within(screen.getByTestId('bulk-actions')).getByRole('button', { name: 'Fulfil…' }))
    const dialog = await screen.findByRole('dialog', { name: 'Fulfil items' })
    expect(within(dialog).getAllByRole('textbox').map((box) => box.getAttribute('aria-label'))).toEqual(['Quantity made of Name Keychain'])
  })

  it('turns the page writes off while a bulk remove runs', async () => {
    let finish: () => void = () => undefined
    vi.spyOn(api, 'deleteOrderItem').mockImplementation(() => new Promise<void>((resolve) => (finish = resolve)))
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('checkbox', { name: 'Select Name Keychain' }))
    await user.click(within(screen.getByTestId('bulk-actions')).getByRole('button', { name: 'Remove…' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add product', hidden: true })).toBeDisabled())
    await act(async () => finish())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add product' })).toBeEnabled())
  })

  it('drops the selection when the order is cancelled', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('checkbox', { name: 'Select Name Keychain' }))
    expect(screen.getByTestId('bulk-actions')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancel order…' }))
    await user.click(screen.getByRole('button', { name: 'Cancel the order' }))
    await waitFor(() => expect(state.orders.find((o) => o.id === ORDER_DRAFT)?.status).toBe('cancelled'))
    await waitFor(() => expect(screen.queryByTestId('bulk-actions')).toBeNull())
  })

  it('turns Fulfil off, with the reason, when no selected item can be fulfilled now', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(within(row(ITEM_DRAFT)).getByRole('checkbox', { name: 'Select Name Keychain' }))
    const bar = screen.getByTestId('bulk-actions')
    expect(within(bar).getByRole('button', { name: 'Fulfil…' })).toBeDisabled()
    expect(within(bar).getByRole('button', { name: 'Print…' })).toBeDisabled()
    expect(bar).toHaveTextContent('No selected item can be fulfilled or printed now.')
  })

  it('removes the selected items after a confirm: a draft is deleted, the rest cancelled', async () => {
    const custom = await api.addOrderItem(ORDER_DRAFT, { kind: 'custom', title: 'Rush fee', quantity: 1, unit_price_minor: 500 })
    const deleted = vi.spyOn(api, 'deleteOrderItem')
    const cancelled = vi.spyOn(api, 'cancelOrderItem')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(screen.getByRole('checkbox', { name: 'Select all items' }))
    await user.click(within(screen.getByTestId('bulk-actions')).getByRole('button', { name: 'Remove…' }))
    const confirm = await screen.findByRole('alertdialog', { name: 'Remove 2 items?' })
    expect(confirm).toHaveTextContent('Delete Name Keychain')
    expect(confirm).toHaveTextContent('Cancel Rush fee')
    await user.click(within(confirm).getByRole('button', { name: 'Remove' }))
    await waitFor(() => expect(deleted).toHaveBeenCalledWith(ORDER_DRAFT, ITEM_DRAFT))
    expect(cancelled).toHaveBeenCalledWith(ORDER_DRAFT, custom.id)
    await waitFor(() => expect(screen.queryByTestId(`order-item-${ITEM_DRAFT}`)).toBeNull())
  })

  it("opens each selected item's print in turn", async () => {
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row(ITEM_APPROVED)).getByRole('checkbox', { name: /^Select / }))
    await user.click(within(screen.getByTestId('bulk-actions')).getByRole('button', { name: 'Print…' }))
    expect(await screen.findByRole('dialog', { name: 'Print the line item' })).toBeInTheDocument()
  })

  it("selects nothing on a visitor's cart", async () => {
    const cart = seedCart()
    const added = await api.addOrderItem(cart.id, { kind: 'request', title: 'Tag', quantity: 1 })
    render(cart.id)
    await screen.findByTestId(`order-item-${added.id}`)
    expect(screen.queryByRole('checkbox', { name: /^Select/ })).toBeNull()
  })
})

// #5544 — a library file item names its file under its title, by its name when it was added.
describe('OrderItemsTable · library file items', () => {
  beforeEach(() => resetMockState())

  it('shows the library file an item prints', async () => {
    const added = await api.addOrderItem(ORDER_DRAFT, { library_file_id: 120, quantity: 1, title: 'Drawer label' })
    render(ORDER_DRAFT)
    const source = await screen.findByTestId('item-library-file')
    expect(source).toHaveTextContent('Library file in Bambuddy: drawer-label.3mf')
    expect(within(row(added.id)).getByRole('button', { name: 'Drawer label' })).toBeInTheDocument()
    // A template's item names no file.
    expect(within(row(ITEM_DRAFT)).queryByTestId('item-library-file')).toBeNull()
  })
})
