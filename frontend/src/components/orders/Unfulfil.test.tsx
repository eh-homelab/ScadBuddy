import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { mockSettings, resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ORDER_ACCEPTED, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'

function render(route = `/orders/${ORDER_ACCEPTED}`) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route },
  )
}

/** The accepted order's item made in full and accepted: fulfilled, the order ready. */
function fulfil() {
  state.fulfilments.push({
    id: 'f-made',
    order_item_id: ITEM_APPROVED,
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
}

const row = () => screen.getByTestId(`order-item-${ITEM_APPROVED}`)

// #4952: a fulfilled item's row menu and drawer offer Unfulfil, with a confirm step.
describe('Unfulfil', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('is not offered for an item not fulfilled', async () => {
    render()
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    expect(within(row()).queryByRole('button', { name: 'Row actions' })).not.toBeInTheDocument()
  })

  it("unfulfils from the row menu after the confirm, and the order leaves ready", async () => {
    fulfil()
    const unfulfilled = vi.spyOn(api, 'unfulfilOrderItem')
    const { user } = render()
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row()).getByRole('button', { name: 'Row actions' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Unfulfil…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(unfulfilled).not.toHaveBeenCalled()
    await user.click(within(dialog).getByRole('button', { name: 'Unfulfil' }))
    await waitFor(() => expect(unfulfilled).toHaveBeenCalledWith(ORDER_ACCEPTED, ITEM_APPROVED))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    await waitFor(() => expect(state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status).toBe('in_production'))
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('approved')
    expect(state.fulfilments.find((f) => f.id === 'f-made')!.accepted_at).toBeNull()
  })

  it('cancels without a call', async () => {
    fulfil()
    const unfulfilled = vi.spyOn(api, 'unfulfilOrderItem')
    const { user } = render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    await user.click(await screen.findByRole('button', { name: 'Unfulfil…' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Cancel' }))
    expect(unfulfilled).not.toHaveBeenCalled()
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('fulfilled')
  })

  it('is disabled in the drawer with the reason for a shipped item', async () => {
    fulfil()
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
    render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    expect(await screen.findByRole('button', { name: 'Unfulfil…' })).toBeDisabled()
    expect(screen.getByTestId('unfulfil-blocked')).toHaveTextContent('Take it off the shipment first')
  })

  it("shows the backend's refusal in the confirm", async () => {
    fulfil()
    const { user } = render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    await user.click(await screen.findByRole('button', { name: 'Unfulfil…' }))
    // Put on hold meanwhile, by another tab: the backend refuses.
    state.orders.find((o) => o.id === ORDER_ACCEPTED)!.on_hold = true
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Unfulfil' }))
    expect(await within(screen.getByRole('alertdialog')).findByRole('alert')).toHaveTextContent('on hold')
  })

  // #4953: `orders_allow_unfulfil` off disables the action, with a link to the setting.
  it('is disabled with a link to Settings when the setting is off', async () => {
    fulfil()
    mockSettings().orders_allow_unfulfil = false
    render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Unfulfil…' })).toBeDisabled())
    const reason = screen.getByTestId('unfulfil-blocked')
    expect(reason).toHaveTextContent('Turned off in Settings → Orders.')
    expect(within(reason).getByRole('link', { name: 'Open Settings' })).toHaveAttribute('href', '/settings/printing#orders')
  })

  it('disables the row menu entry when the setting is off', async () => {
    fulfil()
    mockSettings().orders_allow_unfulfil = false
    const { user } = render()
    await screen.findByRole('heading', { name: 'ORD-2026-2' })
    await user.click(within(row()).getByRole('button', { name: 'Row actions' }))
    const entry = await screen.findByRole('menuitem', { name: 'Unfulfil…' })
    await waitFor(() => expect(entry).toHaveAttribute('data-disabled'))
    expect(screen.getByRole('menuitem', { name: 'Open Settings → Orders' })).toHaveAttribute('href', '/settings/printing#orders')
  })

  it('the mock refuses with unfulfil_disabled when the setting is off', async () => {
    fulfil()
    mockSettings().orders_allow_unfulfil = false
    await expect(api.unfulfilOrderItem(ORDER_ACCEPTED, ITEM_APPROVED)).rejects.toMatchObject({ status: 409 })
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('fulfilled')
  })
})
