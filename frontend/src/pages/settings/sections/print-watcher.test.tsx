import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { resetMockState } from '../../../mocks/handlers'
import { LAN_PRINTER } from '../../../mocks/features/printWatch'
import { region, renderSettingsSection } from '../testing'
import { Component as PrintWatcher } from './print-watcher'

const save = () => within(region('Print watcher')).getByRole('button', { name: 'Save Print watcher' })

// #4699 phase 2 — the print watcher's three settings.
describe('Print watcher', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  it('starts off, with the archive, its slicer project and the library allowed, and no printer skipped', async () => {
    renderSettingsSection(<PrintWatcher />, { route: '/settings/printing' })
    const on = await screen.findByRole('switch', { name: /Import the prints ScadBuddy did not start/ })
    expect(on).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: "The archive's file" })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: "The archive's slicer project" })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'A library file with the same hash' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: "The printer's storage" })).not.toBeChecked()
    expect(await screen.findByRole('checkbox', { name: 'Skip 3DP-31B-598' })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Skip 3DP-77A-114' })).not.toBeChecked()
  })

  it('saves the switch, the sources and the printers to skip', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<PrintWatcher />, { route: '/settings/printing' })
    await user.click(await screen.findByRole('switch', { name: /Import the prints ScadBuddy did not start/ }))
    await user.click(screen.getByRole('checkbox', { name: "The printer's storage" }))
    await user.click(screen.getByRole('checkbox', { name: "The archive's slicer project" }))
    await user.click(await screen.findByRole('checkbox', { name: 'Skip 3DP-77A-114' }))
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({
      print_auto_import: true,
      print_auto_import_sources: ['archive', 'library', 'printer'],
      print_auto_import_skip_printers: [2],
    })
    const stored = await api.getSettings()
    expect(stored.print_auto_import).toBe(true)
    expect(stored.print_auto_import_skip_printers).toEqual([2])
  })

  // #6041 — a LAN-only printer (no Bambuddy link) is skipped by its registry id.
  it('lists only the LAN-only printers, and saves the ones to skip by registry id', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<PrintWatcher />, { route: '/settings/printing' })
    const lan = await screen.findByRole('group', { name: 'LAN-only printers to skip' })
    const garage = await within(lan).findByRole('checkbox', { name: `Skip ${LAN_PRINTER.name}` })
    // The registry's Bambuddy-linked printer is not a LAN-only one.
    expect(within(lan).queryByRole('checkbox', { name: 'Skip 3DP-31B-598' })).toBeNull()
    expect(garage).not.toBeChecked()
    await user.click(garage)
    await user.click(save())
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({ print_auto_import_skip_lan_printers: [LAN_PRINTER.id] })
    expect((await api.getSettings()).print_auto_import_skip_lan_printers).toEqual([LAN_PRINTER.id])
  })

  it('keeps a skipped printer shown once it has a Bambuddy link, so it can be cleared', async () => {
    const linked = '00000000-0000-4000-8000-000000000001'
    await api.putSettings({ print_auto_import_skip_lan_printers: [linked] })
    renderSettingsSection(<PrintWatcher />, { route: '/settings/printing' })
    const lan = await screen.findByRole('group', { name: 'LAN-only printers to skip' })
    expect(await within(lan).findByRole('checkbox', { name: 'Skip 3DP-31B-598' })).toBeChecked()
  })

  it('warns that the printer source reaches the printer', async () => {
    renderSettingsSection(<PrintWatcher />, { route: '/settings/printing' })
    await screen.findByRole('checkbox', { name: "The printer's storage" })
    expect(screen.getByText(/reads the printer's storage over FTP/)).toBeInTheDocument()
  })
})
