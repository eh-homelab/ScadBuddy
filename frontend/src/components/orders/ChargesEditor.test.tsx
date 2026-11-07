import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { Order, OrderCharge } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { addMode } from './afterAcceptance'
import { ChargesEditor } from './ChargesEditor'

const POST: OrderCharge = { id: 'h0000000-0000-4000-8000-000000000001', order_id: ORDER_ACCEPTED, kind: 'shipping', label: 'Post', amount_minor: 500, position: 0 }

function order(id: string, status: Order['status'], onHold = false) {
  return { id, number: 'ORD-2026-2', status, on_hold: onHold, currency: 'USD', minor_exponent: 2 }
}

function show(o: ReturnType<typeof order>, charges: OrderCharge[] = [POST]) {
  const onChanged = vi.fn()
  const view = renderPage(<ChargesEditor order={o} charges={charges} mode={addMode(o)} onChanged={onChanged} />)
  return { ...view, onChanged }
}

// #5351: the charges of an accepted order change on their own route, after a confirm.
describe('ChargesEditor', () => {
  beforeEach(() => resetMockState())
  // Here, not at the end of a test: a failed assertion must not leak a spy into the next.
  afterEach(() => vi.restoreAllMocks())

  it.each<[Order['status'], RegExp]>([
    ['accepted', /^ORD-2026-2 is accepted\. Changing the charges changes what the customer pays\./],
    ['in_production', /^ORD-2026-2 is in production\. Changing the charges/],
    ['ready', /^ORD-2026-2 is ready\. Changing the charges/],
  ])('confirms a change on a %s order, then saves it on the after-acceptance route', async (status, text) => {
    const plain = vi.spyOn(api, 'updateOrder')
    const after = vi.spyOn(api, 'replaceChargesAfterAcceptance')
    const { user, onChanged } = show(order(ORDER_ACCEPTED, status))
    const region = screen.getByRole('region', { name: 'Charges' })
    await user.click(within(region).getByRole('button', { name: 'Add charge' }))
    await user.type(within(region).getByLabelText('Charge 2 label'), 'Agreed cap')
    await user.type(within(region).getByLabelText('Charge 2 amount'), '-3')
    await user.click(within(region).getByRole('button', { name: 'Save charges' }))

    // The first click only arms the confirm, which names the order's status.
    expect(within(region).getByRole('alert', { name: 'Confirm the change' })).toHaveTextContent(text)
    expect(after).not.toHaveBeenCalled()
    await user.click(within(region).getByRole('button', { name: 'Change the accepted order' }))

    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(after).toHaveBeenCalledWith(ORDER_ACCEPTED, [
      { kind: 'shipping', label: 'Post', amount_minor: 500 },
      { kind: 'shipping', label: 'Agreed cap', amount_minor: -300 },
    ])
    expect(plain).not.toHaveBeenCalled()
  })

  it('asks again after a new edit, and Back drops the confirm', async () => {
    const after = vi.spyOn(api, 'replaceChargesAfterAcceptance')
    const { user } = show(order(ORDER_ACCEPTED, 'accepted'))
    const region = screen.getByRole('region', { name: 'Charges' })
    await user.clear(within(region).getByLabelText('Charge 1 amount'))
    await user.type(within(region).getByLabelText('Charge 1 amount'), '4')
    await user.click(within(region).getByRole('button', { name: 'Save charges' }))
    await user.click(within(region).getByRole('button', { name: 'Back' }))
    expect(within(region).queryByRole('alert', { name: 'Confirm the change' })).toBeNull()
    await user.click(within(region).getByRole('button', { name: 'Save charges' }))
    await user.type(within(region).getByLabelText('Charge 1 label'), ' 2')
    expect(within(region).queryByRole('alert', { name: 'Confirm the change' })).toBeNull()
    expect(within(region).getByRole('button', { name: 'Save charges' })).toBeInTheDocument()
    expect(after).not.toHaveBeenCalled()
  })

  it('asks again after a refused save (#5563)', async () => {
    const after = vi.spyOn(api, 'replaceChargesAfterAcceptance').mockRejectedValueOnce(new Error('order ORD-2026-2 is on hold'))
    const { user } = show(order(ORDER_ACCEPTED, 'accepted'))
    const region = screen.getByRole('region', { name: 'Charges' })
    await user.clear(within(region).getByLabelText('Charge 1 amount'))
    await user.type(within(region).getByLabelText('Charge 1 amount'), '4')
    await user.click(within(region).getByRole('button', { name: 'Save charges' }))
    await user.click(within(region).getByRole('button', { name: 'Change the accepted order' }))
    await waitFor(() => expect(after).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(within(region).queryByRole('alert', { name: 'Confirm the change' })).toBeNull())
    // The retry arms the confirm again instead of sending at once.
    await user.click(within(region).getByRole('button', { name: 'Save charges' }))
    expect(within(region).getByRole('alert', { name: 'Confirm the change' })).toBeInTheDocument()
    expect(after).toHaveBeenCalledTimes(1)
  })

  it('saves a draft order with the plain PATCH and no confirm', async () => {
    const plain = vi.spyOn(api, 'updateOrder')
    const after = vi.spyOn(api, 'replaceChargesAfterAcceptance')
    const { user, onChanged } = show(order(ORDER_DRAFT, 'draft'), [])
    const region = screen.getByRole('region', { name: 'Charges' })
    await user.click(within(region).getByRole('button', { name: 'Add charge' }))
    await user.type(within(region).getByLabelText('Charge 1 label'), 'Post')
    await user.type(within(region).getByLabelText('Charge 1 amount'), '5')
    await user.click(within(region).getByRole('button', { name: 'Save charges' }))
    await waitFor(() => expect(onChanged).toHaveBeenCalled())
    expect(plain).toHaveBeenCalledWith(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 500 }] })
    expect(after).not.toHaveBeenCalled()
  })

  it.each<[Order['status'], boolean]>([
    ['accepted', true],
    ['shipped', false],
    ['closed', false],
  ])('shows the charges read-only on a %s order (on hold: %s)', (status, onHold) => {
    show(order(ORDER_ACCEPTED, status, onHold))
    const region = screen.getByRole('region', { name: 'Charges' })
    expect(within(region).getByText('Post')).toBeInTheDocument()
    expect(within(region).queryByRole('button', { name: 'Save charges' })).toBeNull()
  })
})
