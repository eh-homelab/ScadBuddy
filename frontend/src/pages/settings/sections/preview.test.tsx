import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { getDisplayUnit } from '../../../lib/units'
import { choose, optionsOf } from '../../../test/select'
import { region, renderSettingsSection } from '../testing'
import { Component as Preview } from './preview'
import { Component as Printing } from './printing'
import { Component as Projects } from './projects'

const route = '/settings/library'

/** The section renders once the settings are loaded. */
async function loaded() {
  await screen.findByLabelText('Show dimensions in')
}

function renderAll() {
  return renderSettingsSection(
    <>
      <Printing />
      <Projects />
      <Preview />
    </>,
    { route },
  )
}

describe('Preview', () => {
  it('saves the plate the preview falls back to (#81)', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Preview />, { route })
    await loaded()

    const select = screen.getByLabelText('Default plate')
    await choose(user, select, /A1 mini/)
    await user.click(within(region('Preview')).getByRole('button', { name: 'Save Preview' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toMatchObject({ default_plate: 'A1 mini' })
    expect(await api.getPlate(null)).toMatchObject({ name: 'A1 mini' })
    put.mockRestore()
  })

  it('saves the display unit and switches every open view to it', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderSettingsSection(<Preview />, { route })
    await loaded()

    const select = screen.getByLabelText('Show dimensions in')
    expect(select).toHaveTextContent('Millimetres (mm)')
    expect(screen.getByLabelText('Default plate')).toHaveTextContent('256 × 256 mm')

    await choose(user, select, 'Inches (in)')
    // The plate list follows the picker before the save, so the choice can be judged.
    expect(screen.getByLabelText('Default plate')).toHaveTextContent('10.08 × 10.08 in')
    expect(getDisplayUnit()).toBe('mm')

    await user.click(within(region('Preview')).getByRole('button', { name: 'Save Preview' }))
    await waitFor(() => expect(getDisplayUnit()).toBe('in'))
    expect(put.mock.calls[0]?.[0]).toMatchObject({ display_unit: 'in' })
    expect((await api.getSettings()).display_unit).toBe('in')
    put.mockRestore()
  })

  it('offers the folders and printers Bambuddy reports, and no slicer pipeline', async () => {
    const { user } = renderAll()
    await loaded()
    // The stored ids (folder 2, printer 1) are shown by the names Bambuddy reports.
    await waitFor(() => expect(screen.getByLabelText('Inbox folder, for sends without a project')).toHaveTextContent('ScadBuddy'))
    expect(screen.getByLabelText('Printer')).toHaveTextContent('3DP-31B-598 (H2C)')
    expect(await optionsOf(user, screen.getByLabelText('Printer'))).toEqual([
      'None',
      '3DP-31B-598 (H2C)',
      '3DP-77A-114 (H2C)',
    ])
    // #312: printing is the Print dialog's, which derives its own presets.
    expect(screen.queryByLabelText('Slicer pipeline')).not.toBeInTheDocument()
  })

  it('never sends a pipeline', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderAll()
    await loaded()

    await choose(user, screen.getByLabelText('Printer'), 'None')
    await user.click(within(region('Printing defaults')).getByRole('button', { name: 'Save Printing defaults' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ printer_id: null })
    put.mockRestore()
  })
})
