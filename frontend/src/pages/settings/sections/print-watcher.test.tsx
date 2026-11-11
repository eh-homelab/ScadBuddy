import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { resetMockState } from '../../../mocks/handlers'
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

  it('warns that the printer source reaches the printer', async () => {
    renderSettingsSection(<PrintWatcher />, { route: '/settings/printing' })
    await screen.findByRole('checkbox', { name: "The printer's storage" })
    expect(screen.getByText(/reads the printer's storage over FTP/)).toBeInTheDocument()
  })
})
