import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { resetMockState } from '../../../mocks/handlers'
import { seedOrders, state as orders } from '../../../mocks/features/orders/state'
import { choose } from '../../../test/select'
import { renderSettingsSection } from '../testing'
import { Component as StorefrontPricing } from './storefront-pricing'

const route = '/settings/printing'
/** `mocks/features/printers.ts`: the H2C linked to Bambuddy printer 1. */
const H2C = '00000000-0000-4000-8000-000000000001'

describe('Settings → Storefront pricing (#6153)', () => {
  beforeEach(() => resetMockState())

  it('saves one spool per material', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    await choose(user, within(profile).getByRole('combobox', { name: 'Printer' }), '3DP-31B-598')
    await choose(user, await within(profile).findByRole('combobox', { name: 'Spool for PLA' }), /Hot Pink/)
    await choose(user, within(profile).getByRole('combobox', { name: 'Spool for PETG' }), /Clear/)
    await user.click(within(profile).getByRole('button', { name: 'Save slice profile' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ storefront_slice_profile: { printer: H2C, spools: { pla: 2, petg: 15 } } })
    expect(await within(profile).findByText('Saved.')).toBeInTheDocument()
    put.mockRestore()
  })

  it('lists support materials apart', async () => {
    const pva = { ...orders.filaments[0]!, id: 'f1a00000-0000-4000-8000-0000000000aa', material: 'PVA' }
    seedOrders({ filaments: [...orders.filaments, pva] })
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    await choose(user, within(profile).getByRole('combobox', { name: 'Printer' }), '3DP-31B-598')
    const support = await within(profile).findByRole('group', { name: 'Support (internal)' })
    expect(within(support).getByRole('combobox', { name: 'Spool for PVA' })).toBeInTheDocument()
    const customer = within(profile).getByRole('group', { name: 'Customer materials' })
    expect(within(customer).queryByRole('combobox', { name: 'Spool for PVA' })).not.toBeInTheDocument()
    expect(within(customer).getByRole('combobox', { name: 'Spool for PLA' })).toBeInTheDocument()
  })

  it('asks for a printer before a spool', async () => {
    renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    expect(await within(profile).findByText(/Choose the printer first/)).toBeInTheDocument()
    expect(within(profile).queryByRole('combobox', { name: 'Spool for PLA' })).not.toBeInTheDocument()
  })
})
