import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { api } from '../../../api/client'
import { setMockRemembered } from '../../../mocks/handlers'
import { renderSettingsSection } from '../testing'
import { Component as Remembered } from './remembered'

describe('Remembered choices (#322)', () => {
  it('forgets one remembered choice and leaves the rest', async () => {
    setMockRemembered({
      modelChoices: { 'name-keychain': { tier: 'fine' }, gear: { tier: 'draft' } },
      printerBedTypes: { '1': 'Textured PEI Plate' },
    })
    const { user } = renderSettingsSection(<Remembered />, { route: '/settings/printing' })
    const table = await screen.findByRole('table', { name: 'Remembered choices' })
    expect(within(table).getAllByRole('row')).toHaveLength(4)

    await user.click(within(table).getByRole('button', { name: 'Forget printer and spools for gear' }))
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(3))
    expect(within(table).queryByText('draft quality')).toBeNull()
    expect(within(table).getByText('fine quality')).toBeInTheDocument()
    expect(within(table).getByText('Textured PEI Plate')).toBeInTheDocument()

    await user.click(within(table).getByRole('button', { name: 'Forget printer and spools for name-keychain' }))
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(2))
    expect((await api.getRemembered()).model_print_choices).toEqual({})
    expect((await api.getRemembered()).printer_bed_types).toEqual({ '1': 'Textured PEI Plate' })
  })

  it('shows a remembered print sequence (#4970)', async () => {
    setMockRemembered({ modelChoices: { gear: { tier: 'draft', print_sequence: 'by object' } } })
    renderSettingsSection(<Remembered />, { route: '/settings/printing' })
    const table = await screen.findByRole('table', { name: 'Remembered choices' })
    expect(within(table).getByText('draft quality · by object')).toBeInTheDocument()
  })

  it("shows each project's printer and nozzle and forgets one (#599)", async () => {
    setMockRemembered({
      projectTargets: { '1': { printer_id: 1, nozzle_diameter: '0.4' }, '99': { printer_id: 42 } },
    })
    const { user } = renderSettingsSection(<Remembered />, { route: '/settings/printing' })
    const table = await screen.findByRole('table', { name: 'Remembered choices' })
    const projectRow = (await within(table).findByText('Reagan Keychain')).closest('tr') as HTMLElement
    expect(within(projectRow).getByText(/0\.4 mm/)).toBeInTheDocument()
    const unknownRow = within(table).getByText('Project 99').closest('tr') as HTMLElement
    expect(within(unknownRow).getByText('Printer 42')).toBeInTheDocument()

    await user.click(within(table).getByRole('button', { name: 'Forget project printer and nozzle for Reagan Keychain' }))
    await waitFor(() => expect(within(table).queryByText('Reagan Keychain')).toBeNull())
    expect(within(table).getByText('Project 99')).toBeInTheDocument()
    expect(Object.keys((await api.getRemembered()).project_print_targets ?? {})).toEqual(['99'])
  })

  it('forgets everything after a confirmation', async () => {
    setMockRemembered({ printerBedTypes: { '1': 'Textured PEI Plate' }, modelChoices: { gear: { tier: 'draft' } } })
    const { user } = renderSettingsSection(<Remembered />, { route: '/settings/printing' })
    await screen.findByRole('table', { name: 'Remembered choices' })
    await user.click(screen.getByRole('button', { name: 'Forget all' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Yes, forget all' }))
    expect(await screen.findByText('Nothing is remembered yet.')).toBeInTheDocument()
  })
})
