import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { mockSettings } from '../../../mocks/handlers'
import { server } from '../../../mocks/server'
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

  it('saves the burn window, the lead time and the safety margin with Save Filament (#6269)', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    expect(within(filament).getByLabelText('Burn window, days')).toHaveValue(30)
    expect(within(filament).getByLabelText('Lead time, days')).toHaveValue(7)
    expect(within(filament).getByLabelText('Safety margin, days')).toHaveValue(3)
    await user.clear(within(filament).getByLabelText('Burn window, days'))
    await enter(user, within(filament).getByLabelText('Burn window, days'), '14')
    await user.clear(within(filament).getByLabelText('Lead time, days'))
    await enter(user, within(filament).getByLabelText('Lead time, days'), '21')
    await user.clear(within(filament).getByLabelText('Safety margin, days'))
    await enter(user, within(filament).getByLabelText('Safety margin, days'), '10')
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings()).toMatchObject({ filament_burn_window_days: 14, filament_lead_time_days: 21, filament_safety_margin_days: 10 }))
  })

  it('saves the sync choice as one object per data type', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    await choose(user, within(region('Filament')).getByRole('combobox', { name: 'Spools' }), 'Off')
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_sync).toEqual({ spools: 'off', tags: 'off', weights: 'bb_to_sb' }))
  })

  it('saves the tags choice with Save Filament, as filament_sync.tags beside the spools choice', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await choose(user, within(filament).getByRole('combobox', { name: 'Tags to Bambuddy' }), "Send ScadBuddy's bindings")
    await choose(user, within(filament).getByRole('combobox', { name: 'Spools' }), 'Off')
    // Each choice keeps the other: both parts are one setting until it is saved.
    expect(within(filament).getByRole('combobox', { name: 'Tags to Bambuddy' })).toHaveTextContent("Send ScadBuddy's bindings")
    expect(mockSettings().filament_sync).toEqual({ spools: 'bb_to_sb', tags: 'off', weights: 'bb_to_sb' })
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_sync).toEqual({ spools: 'off', tags: 'sb_to_bb', weights: 'bb_to_sb' }))
  })

  it('saves the weights choice as filament_sync.weights without touching spools or tags', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    expect(within(filament).getByRole('combobox', { name: 'Weights from Bambuddy' })).toHaveTextContent('Bambuddy → ScadBuddy')
    await choose(user, within(filament).getByRole('combobox', { name: 'Weights from Bambuddy' }), 'Off')
    expect(mockSettings().filament_sync).toEqual({ spools: 'bb_to_sb', tags: 'off', weights: 'bb_to_sb' })
    await user.click(within(filament).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().filament_sync).toEqual({ spools: 'bb_to_sb', tags: 'off', weights: 'off' }))
  })

  it('switches the RFID override mode on and says Bambuddy no longer adds unknown spools', async () => {
    let sent: unknown
    server.use(
      http.put('*/api/v1/filament/rfid/override-mode', async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ override_mode: true, state: 'applied', devices: [] })
      }),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('switch', { name: 'RFID override mode' }))
    await waitFor(() => expect(sent).toEqual({ enabled: true }))
    expect(await within(filament).findByText('Bambuddy no longer adds unknown RFID spools')).toBeInTheDocument()
    expect(within(filament).getByRole('switch', { name: 'RFID override mode' })).toBeChecked()
  })

  it('shows why a blocked override mode is blocked and leaves the switch on', async () => {
    server.use(
      http.get('*/api/v1/filament/rfid/status', () =>
        HttpResponse.json({ override_mode: true, state: 'blocked', detail: "Bambuddy's authentication is on, so ScadBuddy cannot change its settings", devices: [] }),
      ),
    )
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    expect(await within(filament).findByText(/Bambuddy's authentication is on/)).toBeInTheDocument()
    expect(within(filament).getByRole('switch', { name: 'RFID override mode' })).toBeChecked()
  })

  it('says a busy update applies on the next refresh', async () => {
    server.use(
      http.get('*/api/v1/filament/rfid/status', () => HttpResponse.json({ override_mode: true, state: 'pending', detail: 'busy', devices: [] })),
    )
    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    expect(await within(region('Filament')).findByText('Another update is running; it applies on the next refresh')).toBeInTheDocument()
  })

  it('applies now and says what the reconcile did', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/reconcile', () =>
        HttpResponse.json({
          mode: { override_mode: true, state: 'applied', devices: [] },
          tags: { pushed: 2, moved: 0, cleared: 0, unchanged: 1, failed: 0 },
          slots: { configured: 1, unchanged: 0, no_preset: 0, no_temps: 3, no_nozzle: 0, failed: 0 },
        }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Apply now' }))
    expect(await within(filament).findByText(/2 tags sent/)).toBeInTheDocument()
    expect(within(filament).getByText(/3 spools have no nozzle temperatures/)).toBeInTheDocument()
  })

  it('says which tags and slots the reconcile could not send', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/reconcile', () =>
        HttpResponse.json({
          mode: { override_mode: true, state: 'applied', devices: [] },
          tags: { pushed: 0, moved: 0, cleared: 0, unchanged: 0, failed: 2 },
          slots: { configured: 0, unchanged: 0, no_preset: 0, no_temps: 0, no_nozzle: 0, failed: 1 },
        }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Apply now' }))
    expect(await within(filament).findByText(/2 tags were not sent/)).toBeInTheDocument()
    expect(within(filament).getByText(/1 slot was not set/)).toBeInTheDocument()
  })

  it('says which slots were skipped for no preset or no nozzle', async () => {
    server.use(
      http.post('*/api/v1/filament/rfid/reconcile', () =>
        HttpResponse.json({
          mode: { override_mode: true, state: 'applied', devices: [] },
          tags: { pushed: 1, moved: 0, cleared: 0, unchanged: 0, failed: 0 },
          slots: { configured: 0, unchanged: 0, no_preset: 2, no_temps: 0, no_nozzle: 1, failed: 0 },
        }),
      ),
    )
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const filament = region('Filament')
    await user.click(await within(filament).findByRole('button', { name: 'Apply now' }))
    expect(await within(filament).findByText(/2 spools have no preset for the nozzle/)).toBeInTheDocument()
    expect(within(filament).getByText(/1 slot: the printer does not report the nozzle for its tray, so it is not set/)).toBeInTheDocument()
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

  it('saves the 3DFP fetch nozzles with Save Filament, and shows them again on the next visit (#6625)', async () => {
    const first = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const nozzles = () => within(region('Filament')).getByRole('group', { name: 'Nozzles to fetch from 3DFP' })
    for (const size of ['0.2', '0.4', '0.6', '0.8']) expect(within(nozzles()).getByRole('checkbox', { name: `${size} mm` })).toBeChecked()
    await first.user.click(within(nozzles()).getByRole('checkbox', { name: '0.2 mm' }))
    await first.user.click(within(nozzles()).getByRole('checkbox', { name: '0.8 mm' }))
    // Nothing moves until the section is saved.
    expect(mockSettings().profile_fetch_nozzles).toEqual(['0.2', '0.4', '0.6', '0.8'])
    await first.user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    await waitFor(() => expect(mockSettings().profile_fetch_nozzles).toEqual(['0.4', '0.6']))
    first.unmount()

    renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.2 mm' })).not.toBeChecked()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.4 mm' })).toBeChecked()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.6 mm' })).toBeChecked()
    expect(within(nozzles()).getByRole('checkbox', { name: '0.8 mm' })).not.toBeChecked()
  })

  it('does not save the 3DFP fetch nozzles with none ticked', async () => {
    const { user } = renderSettingsSection(<Filament />, { route: '/settings/printing' })
    await seeded()
    const nozzles = within(region('Filament')).getByRole('group', { name: 'Nozzles to fetch from 3DFP' })
    for (const size of ['0.2', '0.4', '0.6', '0.8']) await user.click(within(nozzles).getByRole('checkbox', { name: `${size} mm` }))
    await user.click(within(region('Filament')).getByRole('button', { name: 'Save Filament' }))
    expect(await within(region('Filament')).findByText('Choose at least one nozzle.')).toBeInTheDocument()
    expect(mockSettings().profile_fetch_nozzles).toEqual(['0.2', '0.4', '0.6', '0.8'])
  })

  it('has no section for the spool inventory or ScadBuddy\'s spools', () => {
    const titles = SECTION_REGISTRY.map((section) => section.title)
    expect(titles).not.toContain('Spool inventory')
    expect(titles).not.toContain("ScadBuddy's spools")
    expect(SECTION_REGISTRY.find((section) => section.id === 'filament')?.area).toBe('printing')
  })
})
