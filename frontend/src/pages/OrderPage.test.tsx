import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes, useLocation } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HttpResponse, http } from 'msw'
import { api } from '../api/client'
import { server } from '../mocks/server'
import { mockSettings, resetMockState, setMockSettings } from '../mocks/handlers'
import { mockConnectProvider, mockPaymentEvent } from '../mocks/features/payments'
import { choose } from '../test/select'
import { CUSTOMER_BOB, ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, item, seedCart, state } from '../mocks/features/orders/state'
import { renderPage } from '../test/utils'
import { OrderPage } from './OrderPage'

function render(id: string, suffix = '') {
  return renderPage(
    <Routes>
      <Route
        path="/orders/:id"
        element={
          <>
            <OrderPage />
            <Where />
          </>
        }
      />
    </Routes>,
    { route: `/orders/${id}${suffix}` },
  )
}

function Where() {
  const location = useLocation()
  return <output data-testid="where">{`${location.pathname}${location.search}${location.hash}`}</output>
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
    // #4955 step 4: the summary card names each charge on its own line.
    expect(screen.getByRole('heading', { name: 'Summary' })).toBeInTheDocument()
    expect(within(totals).getByText('Shipping · Post')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'History' })).toBeInTheDocument()
  })

  it('shows the money view before accept, and a Loss badge in the header (#5326)', async () => {
    render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    // Design time and test prints come before an accept: the card is there to log them.
    const card = await screen.findByRole('region', { name: 'Cost' })
    expect(within(card).getByRole('form', { name: 'Log time on the order' })).toBeInTheDocument()
    expect(screen.queryByText('Loss')).toBeNull()

    await api.addCostEntry(ORDER_ACCEPTED, { kind: 'other', amount_minor: 5000 })
    render(ORDER_ACCEPTED)
    const heading = await screen.findByRole('heading', { name: 'ORD-2026-2' })
    expect(await within(heading.parentElement!).findByText('Loss')).toBeInTheDocument()
  })

  it('says so when the cost cannot be read, rather than dropping the card (#5326)', async () => {
    server.use(http.get('/api/v1/orders/:id/costs', () => HttpResponse.json({ detail: 'Bambuddy timed out' }, { status: 500 })))
    render(ORDER_ACCEPTED)
    expect(await screen.findByText(/The order's cost could not be read/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeInTheDocument()
  })

  it("shows the lifecycle's refusal naming the items in the way", async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await user.click(screen.getByRole('button', { name: 'Accept order' }))
    const alert = await screen.findByTestId('order-refusal')
    expect(alert).toHaveTextContent(/no accepted quote/)
    expect(alert).toHaveTextContent('Name Keychain')
  })

  it('offers no accept, quote, print, hold or cancel on a storefront cart (#5282)', async () => {
    const cart = seedCart()
    state.items.push(item('cart-item', cart.id, 'draft'))
    const { user } = render(cart.id)
    await screen.findByRole('heading', { name: 'Cart' })
    expect(screen.getByText(/a storefront visitor/)).toBeInTheDocument()
    expect(screen.getByTestId('order-cart')).toHaveTextContent(/not submitted/)
    for (const name of ['Accept order', 'Put on hold', 'Cancel order…', 'Add item', 'Add product', 'Add custom item']) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    // #4955: the row's ⋯ menu offers nothing but the item's details on a cart.
    await user.click(within(screen.getByTestId('order-item-cart-item')).getByRole('button', { name: 'Row actions' }))
    expect((await screen.findAllByRole('menuitem')).map((m) => m.textContent)).toEqual(['Open details'])
    await user.keyboard('{Escape}')
    // Nor does the row edit the visitor's quantity, price or title.
    expect(within(screen.getByTestId('order-item-cart-item')).queryByRole('textbox')).toBeNull()
    expect(within(screen.getByTestId('order-item-cart-item')).queryByRole('button', { name: 'Edit title' })).toBeNull()
    await user.click(within(screen.getByTestId('order-item-cart-item')).getByRole('button', { name: 'Name Keychain' }))
    const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
    expect(within(drawer).queryByTestId('item-section-quote')).toBeNull()
    expect(within(drawer).queryByTestId('item-section-print')).toBeNull()
    expect(within(drawer).queryByRole('region', { name: 'Price' })).toBeNull()
    expect(within(drawer).queryByLabelText(/Price per unit/)).toBeNull()
    expect(within(drawer).queryByTestId('print-item')).toBeNull()
    expect(within(drawer).queryByTestId('test-print-item')).toBeNull()
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
    expect(screen.queryByTestId('bambuddy-project')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Accept order' }))
    await waitFor(() => expect(screen.getByText('Accepted')).toBeInTheDocument())
    expect(screen.getByTestId('order-totals')).toHaveTextContent('25.00')
    // #4253 §8.1 — accepted with Bambuddy linked, the order has its Bambuddy project.
    // No web URL is loaded here, so it is named by its number.
    expect(await screen.findByTestId('bambuddy-project')).toHaveTextContent(/^Bambuddy project #\d+$/)
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
    await screen.findByRole('heading', { name: order.number! })
    expect(screen.getByText(/Add one with Add product above/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add product' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add item' })
    await user.type(within(dialog).getByLabelText('Template'), 'maze')
    await user.click(await within(dialog).findByRole('button', { name: /Ball Maze/ }))
    await waitFor(() => expect(within(dialog).getByRole('radio', { name: 'Current version, default settings' })).toBeChecked())
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Add item' })).not.toBeInTheDocument())
    expect(await within(screen.getByRole('table', { name: 'Line items' })).findByRole('button', { name: 'Ball Maze' })).toBeInTheDocument()
  })

  it('points an empty accepted order at Add product and the custom item row (#5349, #5541)', async () => {
    const order = await api.createOrder({ customer_id: CUSTOMER_BOB })
    const row = state.orders.find((o) => o.id === order.id)
    if (row) row.status = 'accepted'
    render(order.id)
    await screen.findByRole('heading', { name: order.number! })
    expect(screen.getByText('No items yet. Add one with Add product above, or type a custom item below.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add product' })).toBeInTheDocument()
    // The custom row adds on the after-acceptance route, after a confirm.
    expect(screen.getByRole('button', { name: 'Add custom item' })).toBeInTheDocument()
    expect(screen.getByTestId('custom-item-row')).toBeInTheDocument()
  })

  it('points an empty table at Add product only where there is one', async () => {
    const order = await api.createOrder({ customer_id: CUSTOMER_BOB })
    await api.cancelOrder(order.id)
    render(order.id)
    await screen.findByRole('heading', { name: order.number! })
    expect(screen.getByText('No items.')).toBeInTheDocument()
    expect(screen.queryByText(/Add one with Add product above/)).not.toBeInTheDocument()
  })

  it('offers Add product on an accepted order, which asks first and quotes the new item (#5349)', async () => {
    const after = vi.spyOn(api, 'addOrderItemAfterAcceptance')
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(screen.getByRole('button', { name: 'Add product' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add item' })
    await user.click(within(dialog).getByRole('button', { name: 'Design request' }))
    await user.type(within(dialog).getByLabelText('Title'), 'Tag 49')
    await user.click(within(dialog).getByRole('button', { name: 'Add item' }))
    expect(within(dialog).getByRole('alert', { name: 'Confirm the change' })).toHaveTextContent(/ORD-2026-2 is accepted\. Adding an item changes what the customer gets/)
    await user.click(within(dialog).getByRole('button', { name: 'Change the accepted order' }))
    await waitFor(() => expect(after).toHaveBeenCalledWith(ORDER_ACCEPTED, { kind: 'request', title: 'Tag 49', quantity: 1, notes: '' }))
    const added = (await api.order(ORDER_ACCEPTED)).items.find((i) => i.title === 'Tag 49')
    expect(added?.status).toBe('draft')
    // The new item has no accepted price: it still takes a revision, as on a draft order.
    await user.click(within(await screen.findByTestId(`order-item-${added?.id}`)).getByRole('button', { name: 'Tag 49' }))
    const drawer = await screen.findByRole('dialog', { name: 'Tag 49' })
    expect(within(drawer).getByRole('button', { name: 'New revision' })).toBeInTheDocument()
  })

  it('offers the design and filament edits of an item revised after acceptance, never its quantity (#5582)', async () => {
    const approved = state.items.find((i) => i.order_id === ORDER_ACCEPTED && i.status === 'approved')!
    const first = render(ORDER_ACCEPTED, `?item=${approved.id}`)
    let drawer = await screen.findByRole('dialog', { name: approved.title })
    // Accepted: the price is pinned, and a new design goes through the confirm.
    expect(within(drawer).getByLabelText('Quantity')).toBeDisabled()
    expect(within(drawer).queryByRole('button', { name: 'Quote from a slice…' })).toBeNull()
    expect(within(drawer).queryByRole('button', { name: 'Record and accept' })).toBeNull()
    first.unmount()

    await api.addOrderItemRevisionAfterAcceptance(ORDER_ACCEPTED, approved.id, { note: 'loop' })
    render(ORDER_ACCEPTED, `?item=${approved.id}`)
    drawer = await screen.findByRole('dialog', { name: approved.title })
    await within(drawer).findByTestId('revised-after-acceptance')
    // `priceOpen`: the new design takes a quote (its filaments with it) and more revisions, as the backend allows.
    expect(within(drawer).getByRole('button', { name: 'Quote from a slice…' })).toBeEnabled()
    expect(within(drawer).getByRole('button', { name: 'Record and accept' })).toBeInTheDocument()
    expect(within(drawer).getByRole('button', { name: 'New revision' })).toBeEnabled()
    // The quantity stays pinned: it would reprice the old quote.
    expect(within(drawer).getByLabelText('Quantity')).toBeDisabled()
  })

  it('offers no Add product on an accepted order on hold', async () => {
    await api.holdOrder(ORDER_ACCEPTED)
    render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    expect(await screen.findByRole('button', { name: 'Release hold' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Add product' })).not.toBeInTheDocument()
  })

  describe('a link to a line item (#4693)', () => {
    const scrolls = vi.fn()
    beforeEach(() => {
      Element.prototype.scrollIntoView = scrolls
    })
    afterEach(() => {
      scrolls.mockReset()
      // Back to vitest.setup.ts's no-op: a Radix select scrolls its options into view.
      Element.prototype.scrollIntoView = () => {}
    })

    it('opens the order with the item named by ?item= marked and open', async () => {
      render(ORDER_DRAFT, `?item=${ITEM_DRAFT}`)
      expect(await screen.findByRole('dialog', { name: 'Name Keychain' })).toBeInTheDocument()
      const row = screen.getByTestId(`order-item-${ITEM_DRAFT}`)
      expect(row).toHaveAttribute('id', `item-${ITEM_DRAFT}`)
      expect(row).toHaveAttribute('aria-current', 'true')
      await waitFor(() => expect(scrolls).toHaveBeenCalledTimes(1))
      expect(scrolls.mock.contexts[0]).toBe(row)
    })

    it('takes #item-<id> as the same, and moves it into the search on close', async () => {
      const { user } = render(ORDER_DRAFT, `#item-${ITEM_DRAFT}`)
      const drawer = await screen.findByRole('dialog', { name: 'Name Keychain' })
      await user.click(within(drawer).getByRole('button', { name: 'Close' }))
      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Name Keychain' })).toBeNull())
      expect(screen.getByTestId('where')).toHaveTextContent(new RegExp(`^/orders/${ORDER_DRAFT}$`))
    })

    it('puts the open item in the URL, so the link can be copied', async () => {
      const { user } = render(ORDER_DRAFT)
      await screen.findByRole('heading', { name: 'ORD-2026-3' })
      await user.click(within(screen.getByTestId(`order-item-${ITEM_DRAFT}`)).getByRole('button', { name: 'Name Keychain' }))
      await screen.findByRole('dialog', { name: 'Name Keychain' })
      expect(screen.getByTestId('where')).toHaveTextContent(`/orders/${ORDER_DRAFT}?item=${ITEM_DRAFT}`)
      expect(screen.getByTestId(`order-item-${ITEM_DRAFT}`)).toHaveAttribute('aria-current', 'true')
      // A row clicked here is in view already: it does not jump.
      expect(scrolls).not.toHaveBeenCalled()
    })

    it('opens nothing for an item the order does not have, and drops it from the URL', async () => {
      render(ORDER_DRAFT, '?item=not-an-item')
      await screen.findByRole('heading', { name: 'ORD-2026-3' })
      expect(screen.queryByRole('dialog')).toBeNull()
      await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent(new RegExp(`^/orders/${ORDER_DRAFT}$`)))
    })
  })
})

describe('OrderPage payment requests (#4957)', () => {
  beforeEach(() => resetMockState())

  it('records a payment for an open request and closes it', async () => {
    mockConnectProvider()
    const opened = await api.requestPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
    const { user } = render(ORDER_ACCEPTED)
    const requests = await screen.findByRole('list', { name: 'Payment requests' })
    expect(within(requests).getByText('Open')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Received (USD)'), '10.00')
    await choose(user, screen.getByRole('combobox', { name: 'For request' }), /Balance .*10\.00/)
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Payment requests' })).getByText('Paid')).toBeInTheDocument())
    expect(within(screen.getByRole('list', { name: 'Payments' })).getByText('$10.00')).toBeInTheDocument()
    expect((await api.order(ORDER_ACCEPTED)).payment_requests?.find((r) => r.id === opened.id)?.status).toBe('paid')
  })

  it('shows a provider payment as paid by that provider', async () => {
    mockConnectProvider()
    const opened = await api.requestPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'paid')
    render(ORDER_ACCEPTED)
    const payments = await screen.findByRole('list', { name: 'Payments' })
    expect(within(payments).getByText(/Paid by Fake/)).toBeInTheDocument()
  })

  it('offers no request to record against once none is open', async () => {
    mockConnectProvider()
    const opened = await api.requestPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
    await api.cancelPaymentRequest(ORDER_ACCEPTED, opened.id)
    render(ORDER_ACCEPTED)
    await screen.findByLabelText('Received (USD)')
    expect(screen.queryByRole('combobox', { name: 'For request' })).not.toBeInTheDocument()
  })

  it('names a provider in the history, not the browser', async () => {
    mockConnectProvider()
    const opened = await api.requestPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'paid')
    render(ORDER_ACCEPTED)
    const history = await screen.findByRole('region', { name: 'Audit line' })
    const added = (await within(history).findAllByText('payment.added'))[0]!.closest('li')!
    expect(added).toHaveTextContent('Fake (provider)')
    expect(within(history).getByText('cost.added').closest('li')).toHaveTextContent('Fake (provider)')
    expect(within(history).getByText('payment_request.opened').closest('li')).toHaveTextContent('browser')
  })

  it('labels a provider payment with the provider’s own label', async () => {
    mockConnectProvider()
    const opened = await api.requestPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'paid')
    render(ORDER_ACCEPTED)
    expect(within(await screen.findByRole('list', { name: 'Payments' })).getByText(/Paid by Fake/)).toBeInTheDocument()
  })

  it('names the method a provider paid by, such as Venmo through PayPal (#4960)', async () => {
    mockConnectProvider()
    const opened = await api.requestPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
    mockPaymentEvent(ORDER_ACCEPTED, opened.id, 'paid', 'venmo')
    render(ORDER_ACCEPTED)
    expect(within(await screen.findByRole('list', { name: 'Payments' })).getByText(/Paid by Venmo via Fake/)).toBeInTheDocument()
  })

  it('offers the manual methods in a list and records the chosen one (#4960)', async () => {
    const { user } = render(ORDER_ACCEPTED)
    const method = await screen.findByRole('combobox', { name: 'Method' })
    await user.click(method)
    const offered = screen.getAllByRole('option').map((o) => o.textContent)
    expect(offered).toEqual(['Not given', 'Cash', 'Check', 'Bank transfer', 'Venmo', 'Zelle', 'Other'])
    await user.click(screen.getByRole('option', { name: 'Venmo' }))
    await user.type(screen.getByLabelText('Received (USD)'), '4.00')
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    const payments = await screen.findByRole('list', { name: 'Payments' })
    await waitFor(() => expect(within(payments).getByText(/Venmo/)).toBeInTheDocument())
    expect((await api.order(ORDER_ACCEPTED)).payments?.at(-1)?.method).toBe('venmo')
  })

  it('takes a method of its own under Other', async () => {
    const { user } = render(ORDER_ACCEPTED)
    await choose(user, await screen.findByRole('combobox', { name: 'Method' }), 'Other')
    await user.type(screen.getByLabelText('Other method'), 'Gift card')
    await user.type(screen.getByLabelText('Received (USD)'), '2.00')
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    await waitFor(async () => expect((await api.order(ORDER_ACCEPTED)).payments?.at(-1)?.method).toBe('Gift card'))
  })

  it('drops the chosen request from the form once it is no longer open', async () => {
    mockConnectProvider()
    await api.requestPayment(ORDER_ACCEPTED, { amount_minor: 1000 })
    const { user } = render(ORDER_ACCEPTED)
    await choose(user, await screen.findByRole('combobox', { name: 'For request' }), /Balance .*10\.00/)
    await user.type(screen.getByLabelText('Received (USD)'), '5.00')
    await user.click(within(screen.getByRole('list', { name: 'Payment requests' })).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('combobox', { name: 'For request' })).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Record payment' }))
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Payments' })).getByText('$5.00')).toBeInTheDocument())
  })
})

// #5281: a balance request that stands holds the shipment until it is paid or waived.
describe('OrderPage, the balance (#5281)', () => {
  beforeEach(() => resetMockState())

  it('says the balance is unpaid and offers Ship without full payment', async () => {
    const found = state.items.find((i) => i.id === ITEM_APPROVED)!
    found.status = 'fulfilled'
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'ready'
    await api.requestPayment(ORDER_ACCEPTED, { kind: 'balance' })
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const shipments = screen.getByRole('region', { name: 'Shipments' })
    await user.click(within(shipments).getByRole('button', { name: 'Add shipment' }))
    expect(await within(shipments).findByRole('alert')).toHaveTextContent(/balance is not paid/)
    await user.click(within(shipments).getByRole('button', { name: 'Ship without full payment' }))
    await user.type(screen.getByLabelText('Reason'), 'pays on delivery')
    await user.click(screen.getByRole('button', { name: 'Ship unpaid' }))
    await waitFor(() => expect(within(shipments).queryByRole('alert')).toBeNull())
    await user.click(within(shipments).getByRole('button', { name: 'Add shipment' }))
    await waitFor(() => expect(screen.getByText('Shipped')).toBeInTheDocument())
  })

  it('shows the auto tax with its mark, the deposit due and the shipping hold in the summary', async () => {
    await api.requestPayment(ORDER_ACCEPTED, { kind: 'balance' })
    state.charges.push({ id: 'h0000000-0000-4000-8000-0000000000aa', order_id: ORDER_ACCEPTED, kind: 'tax', label: 'Tax 10%', amount_minor: 300, position: 0, auto: true })
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.deposit_minor = 1000
    render(ORDER_ACCEPTED)
    const totals = await screen.findByTestId('order-totals')
    expect(within(totals).getByText('auto')).toBeInTheDocument()
    expect(within(totals).getByText('Deposit due')).toBeInTheDocument()
    expect(totals).toHaveTextContent('$10.00')
    expect(within(totals).getByText('Shipping held until the balance is paid')).toBeInTheDocument()
  })

  // Review fix round 1, minor 1: a waiver made in the Summary card clears the Shipments alert.
  it('clears the unpaid-balance alert once the balance is waived elsewhere on the page', async () => {
    state.items.find((i) => i.id === ITEM_APPROVED)!.status = 'fulfilled'
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status = 'ready'
    await api.requestPayment(ORDER_ACCEPTED, { kind: 'balance' })
    const { user } = render(ORDER_ACCEPTED)
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    const shipments = screen.getByRole('region', { name: 'Shipments' })
    await user.click(within(shipments).getByRole('button', { name: 'Add shipment' }))
    await within(shipments).findByRole('alert')
    const money = screen.getByRole('region', { name: 'Deposit and balance' })
    await user.click(within(money).getByRole('button', { name: 'Ship without full payment' }))
    await user.type(screen.getByLabelText('Reason'), 'pays on delivery')
    await user.click(screen.getByRole('button', { name: 'Ship unpaid' }))
    await waitFor(() => expect(within(shipments).queryByRole('alert')).toBeNull())
  })
})

// Review fix round 1, important 2: the shipping option and the charges editor write one list.
describe('OrderPage, the shipping option and the charges (#5281)', () => {
  beforeEach(() => {
    resetMockState()
    setMockSettings({ ...mockSettings(), storefront_shipping_options: [{ id: 'post', label: 'Tracked post', amount_minor: 650, ships: true }] })
  })

  it('keeps the shipping charge: Add charge, pick an option, Save', async () => {
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const charges = screen.getByRole('region', { name: 'Charges' })
    await user.click(within(charges).getByRole('button', { name: 'Add charge' }))
    // The editor has unsaved rows: the picker waits for them.
    expect(screen.getByRole('combobox', { name: 'Shipping option' })).toBeDisabled()
    expect(screen.getByText('Save or discard the charges first')).toBeInTheDocument()
    await user.click(within(charges).getByRole('button', { name: 'Discard' }))
    await choose(user, screen.getByRole('combobox', { name: 'Shipping option' }), 'Tracked post · $6.50')
    await waitFor(() => expect(within(charges).getByLabelText('Charge 1 label')).toHaveValue('Tracked post'))
    await user.click(within(charges).getByRole('button', { name: 'Add charge' }))
    await choose(user, within(charges).getByRole('combobox', { name: 'Charge 2 kind' }), 'fee')
    await user.type(within(charges).getByLabelText('Charge 2 label'), 'Rush')
    await user.type(within(charges).getByLabelText('Charge 2 amount'), '3')
    await user.click(within(charges).getByRole('button', { name: 'Save charges' }))
    await waitFor(() =>
      expect(state.charges.filter((c) => c.order_id === ORDER_DRAFT).map((c) => [c.kind, c.label, c.amount_minor, c.shipping_option_id ?? null])).toEqual([
        ['shipping', 'Tracked post', 650, 'post'],
        ['fee', 'Rush', 300, null],
      ]),
    )
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Shipping option' })).toBeEnabled())
  })

  // Review fix round 2: no edit starts on the charges while the option is being written.
  it('locks Add charge while the shipping option is being written', async () => {
    let release: () => void = () => {}
    const gate = new Promise<void>((resolve) => (release = resolve))
    server.use(
      http.put('/api/v1/orders/:id/shipping-option', async () => {
        await gate
        return undefined
      }),
    )
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    const charges = screen.getByRole('region', { name: 'Charges' })
    await choose(user, screen.getByRole('combobox', { name: 'Shipping option' }), 'Tracked post · $6.50')
    await waitFor(() => expect(within(charges).getByRole('button', { name: 'Add charge' })).toBeDisabled())
    release()
    await waitFor(() => expect(within(charges).getByLabelText('Charge 1 label')).toHaveValue('Tracked post'))
    expect(within(charges).getByRole('button', { name: 'Add charge' })).toBeEnabled()
  })

  // #5726 review: an option that ships keeps the address; the refusal shows on the page.
  it('shows why the shipping address cannot be cleared while the option ships', async () => {
    await api.setShippingOption(ORDER_DRAFT, 'post')
    const { user } = render(ORDER_DRAFT)
    await screen.findByRole('heading', { name: 'ORD-2026-3' })
    await choose(user, screen.getByRole('combobox', { name: 'Shipping address' }), 'No address')
    expect(await screen.findByText(/ships by Tracked post: it keeps its shipping address/)).toBeInTheDocument()
    expect(state.orders.find((o) => o.id === ORDER_DRAFT)!.shipping_address_id).not.toBeNull()
  })
})

