import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { ITEM_APPROVED, ITEM_DRAFT, ORDER_ACCEPTED, ORDER_DRAFT, state } from '../../mocks/features/orders/state'
import { OrderPage } from '../../pages/OrderPage'
import { renderPage } from '../../test/utils'

vi.mock('../print/PlateScene', () => ({ PlateScene: () => <div data-testid="plate-scene" /> }))

function render(route: string) {
  return renderPage(
    <Routes>
      <Route path="/orders/:id" element={<OrderPage />} />
    </Routes>,
    { route },
  )
}

// #4719 — an approved item of an accepted order prints from its accepted quote: the print
// dialog in its item mode posts the item's print, and the item is queued.
describe('PrintItem', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it("prints an approved item with the dialog's choices, and the item is queued", async () => {
    const printed = vi.spyOn(api, 'printItem')
    const { user } = render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    await user.click(await screen.findByTestId('print-item'))
    const dialog = await screen.findByRole('dialog', { name: 'Print the line item' })
    const run = within(dialog).getByTestId('run-item-print')
    await waitFor(() => expect(run).toBeEnabled())
    await user.click(run)
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Print the line item' })).toBeNull())
    expect(printed).toHaveBeenCalledTimes(1)
    const [orderId, itemId, body] = printed.mock.calls[0]!
    expect([orderId, itemId]).toEqual([ORDER_ACCEPTED, ITEM_APPROVED])
    expect(body.filament_plan.slots?.length).toBeGreaterThan(0)
    // Copies, plates and the project are the item's, never the dialog's.
    expect(body).not.toHaveProperty('copies')
    expect(body).not.toHaveProperty('project_id')
    await waitFor(() => expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('queued'))
    const [attempt] = state.fulfilments.filter((f) => f.order_item_id === ITEM_APPROVED)
    expect(attempt).toMatchObject({ outcome: 'unknown', quantity: 0, copies: 2 })
    expect(attempt?.print_run_id).toBeTruthy()
    expect(state.orders.find((o) => o.id === ORDER_ACCEPTED)!.status).toBe('in_production')
    // Printing already: no second print is offered.
    await waitFor(() => expect(screen.queryByTestId('print-item')).toBeNull())
  })

  it("keeps the dialog open with the server's refusal", async () => {
    const { user } = render(`/orders/${ORDER_ACCEPTED}?item=${ITEM_APPROVED}`)
    await user.click(await screen.findByTestId('print-item'))
    const dialog = await screen.findByRole('dialog', { name: 'Print the line item' })
    // The accepted quote is gone meanwhile: the item wants a new one.
    for (const quote of state.quotes) if (quote.order_item_id === ITEM_APPROVED) quote.accepted = false
    const run = within(dialog).getByTestId('run-item-print')
    await waitFor(() => expect(run).toBeEnabled())
    await user.click(run)
    expect(await within(dialog).findByTestId('quote-error')).toHaveTextContent('needs a new quote')
    expect(state.items.find((i) => i.id === ITEM_APPROVED)!.status).toBe('approved')
  })

  it('is not offered before the order is accepted', async () => {
    render(`/orders/${ORDER_DRAFT}?item=${ITEM_DRAFT}`)
    await screen.findByRole('region', { name: 'Fulfilment' })
    expect(screen.queryByTestId('print-item')).toBeNull()
  })
})
