import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { mockSettings } from '../../../mocks/handlers'
import { choose } from '../../../test/select'
import { enter, region, renderSettingsSection } from '../testing'
import { SECTION_REGISTRY } from '../registry'
import { Component as Filament } from './filament'

async function seeded() {
  await screen.findByLabelText('Low stock below, percent')
}

describe('Filament (#6262)', () => {
  it('shows the sync choice, the interval, the thresholds and the currency', async () => {
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    expect(within(filament).getByRole('combobox', { name: 'Spools' })).toHaveTextContent('Bambuddy → ScadBuddy')
    expect(within(filament).getByLabelText('Pull from Bambuddy every')).toHaveValue(300)
    expect(within(filament).getByLabelText('Low stock below, percent')).toHaveValue(20)
    expect(within(filament).getByLabelText('Default cost per kg')).toHaveValue('')
    expect(within(filament).getByText('USD')).toBeInTheDocument()
    expect(within(filament).getByRole('link', { name: 'Change it in Orders' })).toHaveAttribute(
      'href',
      expect.stringContaining('/settings/printing#orders'),
    )
    expect(within(filament).getByRole('link', { name: 'Filament' })).toHaveAttribute('href', expect.stringContaining('/filament'))
  })

  it('saves the sync choice as one object per data type', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    await choose(user, within(region('Filament')).getByRole('combobox', { name: 'Spools' }), 'Off')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_sync).toEqual({ spools: 'off' }))
  })

  it('saves the thresholds and shows the server refusing 101 percent', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const percent = () => within(region('Filament')).getByLabelText('Low stock below, percent')
    await user.clear(percent())
    await enter(user, percent(), '101')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await within(region('Filament')).findByRole('alert')
    expect(mockSettings().filament_low_stock_pct).toBe(20)

    await user.clear(percent())
    await enter(user, percent(), '35')
    await enter(user, within(region('Filament')).getByLabelText('Default cost per kg'), '24.50')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_low_stock_pct).toBe(35))
    expect(mockSettings().filament_default_cost_per_kg).toBe('24.50')
  })

  it('has no section for the spool inventory or ScadBuddy\'s spools', () => {
    const titles = SECTION_REGISTRY.map((section) => section.title)
    expect(titles).not.toContain('Spool inventory')
    expect(titles).not.toContain("ScadBuddy's spools")
    expect(SECTION_REGISTRY.find((section) => section.id === 'filament')?.area).toBe('printing')
  })
})
