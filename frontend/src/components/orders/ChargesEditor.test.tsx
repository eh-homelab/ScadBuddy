import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import type { Order, OrderCharge } from '../../api/types'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT, state } from '../../mocks/features/orders/state'
import { useAsync } from '../../lib/useAsync'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { addMode } from './afterAcceptance'
import { ChargesEditor } from './ChargesEditor'

const POST: OrderCharge = { id: 'h0000000-0000-4000-8000-000000000001', order_id: ORDER_ACCEPTED, kind: 'shipping', label: 'Post', amount_minor: 500, position: 0, auto: false }

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

  // #5281: ScadBuddy computes the auto tax row; the editor shows it and never sends it.
  describe('the auto tax', () => {
    function Host() {
      const view = useAsync(() => api.order(ORDER_DRAFT), [])
      if (!view.data) return null
      const o = view.data.order
      return <ChargesEditor order={o} charges={view.data.charges ?? []} mode={addMode(o)} onChanged={view.refresh} />
    }

    it('shows the auto tax read-only and keeps it on save', async () => {
      setMockSettings({ ...mockSettings(), orders_tax_percent: '10', orders_tax_shipping: true })
      await api.updateOrder(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 1000 }] })
      const plain = vi.spyOn(api, 'updateOrder')
      const { user } = renderPage(<Host />)
      const region = await screen.findByRole('region', { name: 'Charges' })
      const auto = await within(region).findByTestId('auto-tax')
      expect(auto).toHaveTextContent('Tax 10%')
      expect(auto).toHaveTextContent('auto')
      expect(auto).toHaveTextContent('$1.00')
      // One editable row, the shipping; the tax is not one of them.
      expect(within(region).queryByLabelText('Charge 2 label')).toBeNull()

      await user.clear(within(region).getByLabelText('Charge 1 amount'))
      await user.type(within(region).getByLabelText('Charge 1 amount'), '20')
      await user.click(within(region).getByRole('button', { name: 'Save charges' }))
      await waitFor(() => expect(plain).toHaveBeenCalledWith(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 2000 }] }))
      // Recomputed on the new shipping.
      await waitFor(() => expect(within(region).getByTestId('auto-tax')).toHaveTextContent('$2.00'))
      expect(state.charges.filter((c) => c.order_id === ORDER_DRAFT && c.auto).map((c) => c.amount_minor)).toEqual([200])
    })

    it("keeps a charge's shipping option on save", async () => {
      setMockSettings({ ...mockSettings(), storefront_shipping_options: [{ id: 'post', label: 'Tracked post', amount_minor: 650, ships: true }] })
      await api.setShippingOption(ORDER_DRAFT, 'post')
      const plain = vi.spyOn(api, 'updateOrder')
      const { user } = renderPage(<Host />)
      const region = await screen.findByRole('region', { name: 'Charges' })
      await user.click(await within(region).findByRole('button', { name: 'Add charge' }))
      await user.type(within(region).getByLabelText('Charge 2 label'), 'Rush')
      await user.type(within(region).getByLabelText('Charge 2 amount'), '3')
      await user.click(within(region).getByRole('button', { name: 'Save charges' }))
      await waitFor(() =>
        expect(plain).toHaveBeenCalledWith(ORDER_DRAFT, {
          charges: [
            { kind: 'shipping', label: 'Tracked post', amount_minor: 650, shipping_option_id: 'post' },
            { kind: 'shipping', label: 'Rush', amount_minor: 300 },
          ],
        }),
      )
    })

    // Review fix round 1, important 1: the backend drops a typed tax with the auto row's label.
    it("refuses a typed tax with the auto row's label until it has another", async () => {
      setMockSettings({ ...mockSettings(), orders_tax_percent: '10', orders_tax_shipping: true })
      await api.updateOrder(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 1000 }] })
      const plain = vi.spyOn(api, 'updateOrder')
      const { user } = renderPage(<Host />)
      const region = await screen.findByRole('region', { name: 'Charges' })
      await within(region).findByTestId('auto-tax')
      await user.click(within(region).getByRole('button', { name: 'Add charge' }))
      await choose(user, within(region).getByRole('combobox', { name: 'Charge 2 kind' }), 'tax')
      await user.type(within(region).getByLabelText('Charge 2 label'), 'Tax 10%')
      await user.type(within(region).getByLabelText('Charge 2 amount'), '0')
      expect(within(region).getByRole('alert', { name: 'Charge 2 label problem' })).toHaveTextContent('A typed tax needs a label other than “Tax 10%”, the automatic tax; this tax then replaces it.')
      expect(within(region).getByRole('button', { name: 'Save charges' })).toBeDisabled()
      await user.clear(within(region).getByLabelText('Charge 2 label'))
      await user.type(within(region).getByLabelText('Charge 2 label'), 'Sales tax')
      expect(within(region).queryByRole('alert', { name: 'Charge 2 label problem' })).toBeNull()
      await user.click(within(region).getByRole('button', { name: 'Save charges' }))
      await waitFor(() => expect(plain).toHaveBeenCalledWith(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 1000 }, { kind: 'tax', label: 'Sales tax', amount_minor: 0 }] }))
    })

    // Review fix round 1, minor 5: a charge edited away from its option's price no longer claims it.
    it("drops the shipping option from a charge whose label or amount is edited", async () => {
      setMockSettings({ ...mockSettings(), storefront_shipping_options: [{ id: 'post', label: 'Tracked post', amount_minor: 650, ships: true }] })
      await api.setShippingOption(ORDER_DRAFT, 'post')
      const plain = vi.spyOn(api, 'updateOrder')
      const { user } = renderPage(<Host />)
      const region = await screen.findByRole('region', { name: 'Charges' })
      const amount = await within(region).findByLabelText('Charge 1 amount')
      await user.clear(amount)
      await user.type(amount, '5')
      await user.click(within(region).getByRole('button', { name: 'Save charges' }))
      await waitFor(() => expect(plain).toHaveBeenCalledWith(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Tracked post', amount_minor: 500 }] }))
    })

    it('discards unsaved edits', async () => {
      const dirty = vi.fn()
      const o = { ...order(ORDER_DRAFT, 'draft') }
      const { user } = renderPage(<ChargesEditor order={o} charges={[]} mode={addMode(o)} onChanged={() => {}} onDirtyChange={dirty} />)
      const region = screen.getByRole('region', { name: 'Charges' })
      await user.click(within(region).getByRole('button', { name: 'Add charge' }))
      expect(dirty).toHaveBeenLastCalledWith(true)
      await user.click(within(region).getByRole('button', { name: 'Discard' }))
      expect(within(region).queryByLabelText('Charge 1 label')).toBeNull()
      expect(dirty).toHaveBeenLastCalledWith(false)
    })
  })
})

