import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api } from '../../../api/client'
import { mockSettings } from '../../../mocks/handlers'
import { choose } from '../../../test/select'
import { enter, region, renderSettingsSection } from '../testing'
import { Component as Orders } from './orders'

/** The form seeds itself from the server, so wait for the section to arrive. */
async function seeded() {
  await screen.findByLabelText('Orders currency')
}

describe('Orders (#4249)', () => {
  it('shows the orders currency, USD by default, in its own section', async () => {
    renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const orders = region('Orders')
    expect(within(orders).getByLabelText('Orders currency')).toHaveValue('USD')
    // Applies to the next order, never a restart: no restart badge.
    expect(within(orders).queryByText(/restart/i)).toBeNull()
  })

  it('saves a three-letter code and shows the server refusing a lowercase one', async () => {
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const field = () => within(region('Orders')).getByLabelText('Orders currency')
    await user.clear(field())
    await enter(user, field(), 'usd')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await within(region('Orders')).findByRole('alert')
    expect(mockSettings().orders_currency).toBe('USD')

    await user.clear(field())
    await enter(user, field(), 'EUR')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().orders_currency).toBe('EUR'))
    expect(mockSettings().sources?.['orders_currency']).toBe('stored')
  })
})

describe('Orders, the price book (#4250)', () => {
  it('shows the pricing policy and the filaments in the Orders section', async () => {
    renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const orders = region('Orders')
    expect(within(orders).getByLabelText('Machine rate per hour')).toHaveValue('0')
    expect(within(orders).getByLabelText('Setup fee per plate')).toHaveValue('0')
    expect(within(orders).getByLabelText('Labour per item')).toHaveValue('0')
    expect(within(orders).getByLabelText('Markup, percent')).toHaveValue('0')
    expect(within(orders).getByRole('combobox', { name: 'Round the unit price' })).toHaveTextContent('Each line to the cent')
    const filaments = await within(orders).findByRole('table', { name: 'Filaments' })
    expect(await within(filaments).findByText(/Sunflower Yellow/)).toBeInTheDocument()
  })

  it('saves the policy, and shows the server refusing a negative rate', async () => {
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const field = () => within(region('Orders')).getByLabelText('Markup, percent')
    await user.clear(field())
    await enter(user, field(), '-1')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await within(region('Orders')).findByRole('alert')
    expect(mockSettings().pricing_markup_percent).toBe('0')

    await user.clear(field())
    await enter(user, field(), '15')
    await choose(user, within(region('Orders')).getByRole('combobox', { name: 'Round the unit price' }), 'Up to a whole unit')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().pricing_markup_percent).toBe('15'))
    expect(mockSettings().pricing_rounding).toBe('to_major')
    expect((await api.priceBook()).policy.markup_percent).toBe('15')
  })
})

describe('Orders, Unfulfil (#4953)', () => {
  it('turns Unfulfil off and saves it', async () => {
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const toggle = within(region('Orders')).getByRole('switch', { name: 'Allow Unfulfil on a line item' })
    expect(toggle).toBeChecked()
    await user.click(toggle)
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().orders_allow_unfulfil).toBe(false))
    expect(mockSettings().sources?.['orders_allow_unfulfil']).toBe('stored')
  })
})
