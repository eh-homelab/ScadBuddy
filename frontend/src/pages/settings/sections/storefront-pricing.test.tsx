import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { mockSettings, resetMockState, setMockSettings } from '../../../mocks/handlers'
import { T } from '../../../mocks/features/filament/seed'
import { seedOrders, state as orders } from '../../../mocks/features/orders/state'
import { choose } from '../../../test/select'
import { renderSettingsSection } from '../testing'
import { Component as StorefrontPricing } from './storefront-pricing'

const route = '/settings/printing'
/** `mocks/features/printers.ts`: the H2C linked to Bambuddy printer 1. */
const H2C = '00000000-0000-4000-8000-000000000001'
/** `mocks/features/printers.ts`: the printer known by serial only. */
const GARAGE = '00000000-0000-4000-8000-0000000000aa'

describe('Settings → Storefront pricing (#6153, #6574)', () => {
  beforeEach(() => resetMockState())

  it('saves one filament type per material', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    await choose(user, within(profile).getByRole('combobox', { name: 'Printer for storefront prices' }), '3DP-31B-598')
    await choose(user, await within(profile).findByRole('combobox', { name: 'Filament type for PLA' }), /Hot Pink/)
    await choose(user, within(profile).getByRole('combobox', { name: 'Filament type for PETG' }), /Translucent Orange/)
    await user.click(within(profile).getByRole('button', { name: 'Save slice profile' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ storefront_slice_profile: { printer: H2C, materials: { pla: T.PINK, petg: T.ORANGE } } })
    expect(await within(profile).findByText('Saved.')).toBeInTheDocument()
    put.mockRestore()
  })

  it("offers a material's own types", async () => {
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    await user.click(await within(profile).findByRole('combobox', { name: 'Filament type for PETG' }))
    expect((await screen.findAllByRole('option')).map((option) => option.textContent)).toEqual([
      'BL-PETG-HF-TRANSLUCENT-ORANGE PETG Translucent Orange',
      'INS-PETG-CLEAR PETG Clear',
      'PM-PETG-CLEAR PETG Clear',
    ])
  })

  it('lists support materials apart', async () => {
    const pva = { ...orders.filaments[0]!, id: 'f1a00000-0000-4000-8000-0000000000aa', material: 'PVA' }
    seedOrders({ filaments: [...orders.filaments, pva] })
    renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    const support = await within(profile).findByRole('group', { name: 'Support (internal)' })
    expect(within(support).getByRole('combobox', { name: 'Filament type for PVA' })).toBeInTheDocument()
    const customer = within(profile).getByRole('group', { name: 'Customer materials' })
    expect(within(customer).queryByRole('combobox', { name: 'Filament type for PVA' })).not.toBeInTheDocument()
    expect(within(customer).getByRole('combobox', { name: 'Filament type for PLA' })).toBeInTheDocument()
  })

  it('keeps the types when the printer changes', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    await choose(user, within(profile).getByRole('combobox', { name: 'Printer for storefront prices' }), '3DP-31B-598')
    await choose(user, await within(profile).findByRole('combobox', { name: 'Filament type for PLA' }), /Jade White/)
    await choose(user, within(profile).getByRole('combobox', { name: 'Printer for storefront prices' }), 'Garage')
    await user.click(within(profile).getByRole('button', { name: 'Save slice profile' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ storefront_slice_profile: { printer: GARAGE, materials: { pla: T.JADE } } })
    put.mockRestore()
  })

  it('never sends a type for a material the editor does not list, or a type it does not offer', async () => {
    setMockSettings({ ...mockSettings(), storefront_slice_profile: { printer: H2C, materials: { pla: T.JADE, nylon: T.JADE, petg: 'gone-type' } } })
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    await within(profile).findByRole('combobox', { name: 'Filament type for PLA' })
    await user.click(within(profile).getByRole('button', { name: 'Save slice profile' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ storefront_slice_profile: { printer: H2C, materials: { pla: T.JADE } } })
    put.mockRestore()
  })

  it('keeps Save off until the types are read', async () => {
    setMockSettings({ ...mockSettings(), storefront_slice_profile: { printer: H2C, materials: { pla: T.JADE } } })
    const real = api.storefrontSliceOptions.bind(api)
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    const options = vi.spyOn(api, 'storefrontSliceOptions').mockImplementation(async () => {
      await held
      return real()
    })
    renderSettingsSection(<StorefrontPricing />, { route })
    const profile = await screen.findByRole('region', { name: 'Shop slice profile' })
    const save = within(profile).getByRole('button', { name: 'Save slice profile' })
    await waitFor(() => expect(options).toHaveBeenCalled())
    expect(save).toBeDisabled()
    release()
    await within(profile).findByRole('combobox', { name: 'Filament type for PLA' })
    await waitFor(() => expect(save).toBeEnabled())
    options.mockRestore()
  })
})

describe('Settings → Storefront pricing: Storefront materials (#6258)', () => {
  beforeEach(() => resetMockState())

  const rows = (section: HTMLElement) => within(section).getAllByTestId('material-row').map((row) => row.getAttribute('data-material'))

  it('shows the shop default in order, reorders, adds and removes, and saves the list', async () => {
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const section = await screen.findByRole('region', { name: 'Storefront materials' })
    await waitFor(() => expect(rows(section)).toEqual(['PETG', 'PLA']))
    await user.click(within(section).getByRole('button', { name: 'Move PLA up' }))
    expect(rows(section)).toEqual(['PLA', 'PETG'])
    // By keyboard: the moved row's other button has the focus.
    expect(within(section).getByRole('button', { name: 'Move PLA down' })).toHaveFocus()
    await user.click(within(section).getByRole('button', { name: 'Remove PETG' }))
    expect(rows(section)).toEqual(['PLA'])
    expect(within(section).getByRole('button', { name: 'Remove PLA' })).toBeDisabled()
    // The price book's materials not on the list.
    await choose(user, within(section).getByRole('combobox', { name: 'Add a material' }), 'PETG')
    expect(rows(section)).toEqual(['PLA', 'PETG'])
    await user.click(within(section).getByRole('button', { name: 'Save materials' }))
    expect(await within(section).findByText('Saved.')).toBeInTheDocument()
    expect(mockSettings().storefront_materials).toEqual(['PLA', 'PETG'])
  })

  it('says why a save is refused', async () => {
    vi.spyOn(api, 'putSettings').mockRejectedValueOnce(new Error('SCADBUDDY_STOREFRONT_MATERIALS needs at least one material'))
    const { user } = renderSettingsSection(<StorefrontPricing />, { route })
    const section = await screen.findByRole('region', { name: 'Storefront materials' })
    await waitFor(() => expect(rows(section)).toEqual(['PETG', 'PLA']))
    await user.click(within(section).getByRole('button', { name: 'Save materials' }))
    expect(await within(section).findByRole('alert')).toHaveTextContent('needs at least one material')
    vi.restoreAllMocks()
  })
})
