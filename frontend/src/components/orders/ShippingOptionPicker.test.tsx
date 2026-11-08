import { screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import type { ShippingOption } from '../../api/types'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { ORDER_ACCEPTED, ORDER_DRAFT, state } from '../../mocks/features/orders/state'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { useAsync } from '../../lib/useAsync'
import { ShippingOptionPicker } from './ShippingOptionPicker'

const OPTIONS: ShippingOption[] = [
  { id: 'post', label: 'Tracked post', amount_minor: 650, ships: true },
  { id: 'pickup', label: 'Pick up at the shop', amount_minor: 0, ships: false },
]

function Host({ id }: { id: string }) {
  const view = useAsync(() => api.order(id), [id])
  if (!view.data) return null
  return <ShippingOptionPicker order={view.data.order} charges={view.data.charges ?? []} options={OPTIONS} onChanged={view.refresh} />
}

const shipping = (orderId: string) => state.charges.filter((c) => c.order_id === orderId && c.kind === 'shipping')

describe('ShippingOptionPicker (#5281)', () => {
  beforeEach(() => {
    resetMockState()
    setMockSettings({ ...mockSettings(), storefront_shipping_options: OPTIONS })
  })

  it('a pickup needs no address; a shipping option without one is refused', async () => {
    await api.updateOrder(ORDER_DRAFT, { shipping_address_id: null })
    const { user } = renderPage(<Host id={ORDER_DRAFT} />)
    const picker = await screen.findByRole('combobox', { name: 'Shipping option' })

    await choose(user, picker, 'Pick up at the shop · pickup, no address')
    await waitFor(() => expect(shipping(ORDER_DRAFT).map((c) => [c.label, c.amount_minor, c.shipping_option_id])).toEqual([['Pick up at the shop', 0, 'pickup']]))

    await choose(user, screen.getByRole('combobox', { name: 'Shipping option' }), 'Tracked post · $6.50')
    expect(await screen.findByRole('alert')).toHaveTextContent(/has no shipping address: Tracked post ships it/)
    expect(shipping(ORDER_DRAFT).map((c) => c.shipping_option_id)).toEqual(['pickup'])
  })

  it('sets a shipping option with an address, and removes it', async () => {
    const { user } = renderPage(<Host id={ORDER_DRAFT} />)
    await choose(user, await screen.findByRole('combobox', { name: 'Shipping option' }), 'Tracked post · $6.50')
    await waitFor(() => expect(shipping(ORDER_DRAFT).map((c) => c.shipping_option_id)).toEqual(['post']))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Shipping option' })).toHaveTextContent('Tracked post'))
    await choose(user, screen.getByRole('combobox', { name: 'Shipping option' }), 'No shipping option')
    await waitFor(() => expect(shipping(ORDER_DRAFT)).toEqual([]))
  })

  it('shows the option, not a picker, once accepted', async () => {
    renderPage(<Host id={ORDER_ACCEPTED} />)
    await screen.findByText('Shipping option')
    expect(screen.queryByRole('combobox', { name: 'Shipping option' })).toBeNull()
  })

  // Review fix round 1, minor 4: an option Settings no longer lists is still named.
  it('names an option the settings no longer list', async () => {
    setMockSettings({ ...mockSettings(), storefront_shipping_options: [...OPTIONS, { id: 'courier', label: 'Courier', amount_minor: 900, ships: false }] })
    await api.setShippingOption(ORDER_DRAFT, 'courier')
    renderPage(<Host id={ORDER_DRAFT} />)
    expect(await screen.findByRole('combobox', { name: 'Shipping option' })).toHaveTextContent('courier (removed)')
  })

  it('stays off with a note while the charges have unsaved edits', async () => {
    renderPage(<ShippingOptionPicker order={{ id: ORDER_DRAFT, status: 'draft', currency: 'USD', minor_exponent: 2, source: 'back_office', submitted_at: null }} charges={[]} options={OPTIONS} onChanged={() => {}} disabled />)
    expect(await screen.findByRole('combobox', { name: 'Shipping option' })).toBeDisabled()
    expect(screen.getByText('Save or discard the charges first')).toBeInTheDocument()
  })
})

