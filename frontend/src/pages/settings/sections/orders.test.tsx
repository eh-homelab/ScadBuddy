import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
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

describe('Orders, job cost (#5326)', () => {
  it('saves the labour rate and the thin threshold, and refuses a USD rate of 0', async () => {
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const field = (label: string) => within(region('Orders')).getByLabelText(label)
    expect(field('Labour rate per hour')).toHaveValue('0')
    expect(field('Thin margin below, percent')).toHaveValue('15')

    await user.clear(field('Labour rate per hour'))
    await enter(user, field('Labour rate per hour'), '25')
    await user.clear(field('Thin margin below, percent'))
    await enter(user, field('Thin margin below, percent'), '20')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().pricing_labour_rate_per_hour).toBe('25'))
    expect(mockSettings().orders_thin_margin_percent).toBe('20')

    await expect(api.putSettings({ pricing_usd_rate: '0' })).rejects.toThrow()
    expect(mockSettings().pricing_usd_rate).toBe('1')
  })
})

describe('Orders, money rules (#5281)', () => {
  it('saves the tax, the deposit policy and the shipping options', async () => {
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const orders = region('Orders')
    const field = (label: string) => within(orders).getByLabelText(label)
    await user.clear(field('Tax, percent'))
    await enter(user, field('Tax, percent'), '8.25')
    await user.click(within(orders).getByRole('switch', { name: 'Tax shipping too' }))
    await user.click(within(orders).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().orders_tax_percent).toBe('8.25'))
    expect(mockSettings().orders_tax_shipping).toBe(true)

    const policy = await within(orders).findByRole('region', { name: 'Deposit policy' })
    await user.click(within(policy).getByRole('button', { name: 'Add rule' }))
    // A design request has no estimate: a percent of it is zero, so the note says so.
    expect(within(policy).getByText('A design request has no estimate, so a percent counts it as zero: use a fixed amount')).toBeInTheDocument()
    await choose(user, within(policy).getByRole('combobox', { name: 'Rule 1 criterion' }), 'Total above')
    expect(within(policy).queryByText(/A design request has no estimate/)).toBeNull()
    await user.type(within(policy).getByLabelText('Rule 1 above (USD)'), '100')
    await choose(user, within(policy).getByRole('combobox', { name: 'Rule 1 amount type' }), 'Percent')
    await user.type(within(policy).getByLabelText('Rule 1 percent'), '50')
    await user.click(within(policy).getByRole('button', { name: 'Save deposit policy' }))
    await waitFor(() => expect(mockSettings().orders_deposit_policy).toEqual([{ criterion: 'total_above', above_minor: 10000, percent: '50' }]))

    const options = within(orders).getByRole('region', { name: 'Shipping options' })
    await user.click(within(options).getByRole('button', { name: 'Add option' }))
    await user.type(within(options).getByLabelText('Option 1 id'), 'pickup')
    await user.type(within(options).getByLabelText('Option 1 label'), 'Pick up at the shop')
    await user.type(within(options).getByLabelText('Option 1 price (USD)'), '0')
    await user.click(within(options).getByRole('checkbox', { name: 'Option 1 ships' }))
    await user.click(within(options).getByRole('button', { name: 'Add option' }))
    await user.type(within(options).getByLabelText('Option 2 id'), 'post')
    await user.type(within(options).getByLabelText('Option 2 label'), 'Tracked post')
    await user.type(within(options).getByLabelText('Option 2 price (USD)'), '6.50')
    await user.click(within(options).getByRole('button', { name: 'Save shipping options' }))
    await waitFor(() =>
      expect(mockSettings().storefront_shipping_options).toEqual([
        { id: 'pickup', label: 'Pick up at the shop', amount_minor: 0, ships: false },
        { id: 'post', label: 'Tracked post', amount_minor: 650, ships: true },
      ]),
    )
  })

  it('refuses a fixed rule with no amount before it saves', async () => {
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const policy = await within(region('Orders')).findByRole('region', { name: 'Deposit policy' })
    await user.click(within(policy).getByRole('button', { name: 'Add rule' }))
    await user.click(within(policy).getByRole('button', { name: 'Save deposit policy' }))
    expect(await within(policy).findByRole('alert')).toHaveTextContent('Rule 1: enter a fixed amount, such as 20.00.')
    expect(mockSettings().orders_deposit_policy).toEqual([])
  })
})

describe('the storefront bot check (#5283)', () => {
  it('saves the site key and a write-only secret, and never shows the secret', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const orders = region('Orders')
    await user.type(within(orders).getByLabelText('Storefront bot check: Turnstile site key'), '0x4AAA-site')
    await user.type(within(orders).getByLabelText('Storefront bot check: Turnstile secret key'), '0x4AAA-secret')
    await user.click(within(orders).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({
      storefront_turnstile_site_key: '0x4AAA-site',
      storefront_turnstile_secret: '0x4AAA-secret',
    })
    // The server reports only that a secret is set.
    await waitFor(() => expect(mockSettings().has_storefront_turnstile_secret).toBe(true))
    expect(mockSettings()).not.toHaveProperty('storefront_turnstile_secret')
    put.mockRestore()
  })
})

describe('Orders, the handling fee on bought items (#6133)', () => {
  it('saves the flat fee and the percent, and shows the server refusing a percent past 100', async () => {
    const { user } = renderSettingsSection(<Orders />, { route: '/settings/printing' })
    await seeded()
    const orders = region('Orders')
    const flat = () => within(orders).getByLabelText('Handling fee per bought item, minor units')
    const percent = () => within(orders).getByLabelText('Handling fee on bought items, percent')
    expect(flat()).toHaveDisplayValue('0')
    expect(percent()).toHaveValue('0')

    await user.clear(percent())
    await enter(user, percent(), '150')
    await user.click(within(orders).getByRole('button', { name: 'Save Orders' }))
    await within(orders).findByRole('alert')
    expect(mockSettings().orders_purchase_fee_percent).toBe('0')

    await user.clear(flat())
    await enter(user, flat(), '150')
    await user.clear(percent())
    await enter(user, percent(), '7.50')
    await user.click(within(orders).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().orders_purchase_fee_percent).toBe('7.50'))
    expect(mockSettings().orders_purchase_fee_flat_minor).toBe(150)
  })
})
