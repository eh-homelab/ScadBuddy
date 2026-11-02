import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { FILAMENT_PLA } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { FilamentsEditor } from './FilamentsEditor'

// #4250 — the price book's filaments, under Settings' Orders section.
describe('FilamentsEditor (#4250)', () => {
  beforeEach(() => resetMockState())

  it('lists, adds, edits and archives the filaments', async () => {
    const { user } = renderPage(<FilamentsEditor />)
    const table = await screen.findByRole('table', { name: 'Filaments' })
    await within(table).findByText(/Sunflower Yellow/)
    const form = screen.getByRole('form', { name: 'Add a filament' })
    await user.type(within(form).getByLabelText('Material'), 'ABS')
    await user.clear(within(form).getByLabelText('Colour'))
    await user.type(within(form).getByLabelText('Colour'), '#123456')
    await user.type(within(form).getByLabelText('Cost per kg'), '22.75')
    await user.click(within(form).getByRole('button', { name: 'Add filament' }))
    await waitFor(async () => expect((await api.priceBook()).filaments.map((f) => f.material)).toContain('ABS'))
    await within(await screen.findByRole('table', { name: 'Filaments' })).findByText('#123456')

    const row = screen.getByTestId(`filament-${FILAMENT_PLA}`)
    await user.click(within(row).getByRole('button', { name: 'Edit' }))
    const edit = screen.getByRole('form', { name: 'Edit filament' })
    await user.clear(within(edit).getByLabelText('Cost per kg'))
    await user.type(within(edit).getByLabelText('Cost per kg'), '30')
    await user.click(within(edit).getByRole('button', { name: 'Save filament' }))
    await waitFor(async () => expect((await api.priceBook()).filaments.find((f) => f.id === FILAMENT_PLA)?.cost_per_kg).toBe('30'))

    await user.click(within(screen.getByTestId(`filament-${FILAMENT_PLA}`)).getByRole('button', { name: /^Archive/ }))
    await waitFor(() => expect(screen.queryByTestId(`filament-${FILAMENT_PLA}`)).toBeNull())
  })

  // #4977 — an archived record comes back with its id, so its quotes keep naming it.
  it('shows the archived filaments and unarchives one', async () => {
    await api.archiveFilament(FILAMENT_PLA)
    const { user } = renderPage(<FilamentsEditor />)
    await within(await screen.findByRole('table', { name: 'Filaments' })).findByText(/Black/)
    expect(screen.queryByTestId(`filament-${FILAMENT_PLA}`)).toBeNull()

    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    const row = await screen.findByTestId(`filament-${FILAMENT_PLA}`)
    expect(within(row).getByText('Archived')).toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: 'Edit' })).toBeNull()
    await user.click(within(row).getByRole('button', { name: /^Unarchive/ }))
    await waitFor(async () => expect((await api.priceBook()).filaments.map((f) => f.id)).toContain(FILAMENT_PLA))
    await waitFor(() => expect(within(screen.getByTestId(`filament-${FILAMENT_PLA}`)).queryByText('Archived')).toBeNull())
  })

  it('says which record holds the identity when an unarchive is refused', async () => {
    const { material, subtype, brand, colour_name, colour, cost_per_kg, density_g_cm3 } = await api.archiveFilament(FILAMENT_PLA)
    await api.createFilament({ material, subtype, brand, colour_name, colour, cost_per_kg, density_g_cm3 })
    const { user } = renderPage(<FilamentsEditor />)
    await screen.findByRole('table', { name: 'Filaments' })
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    const row = await screen.findByTestId(`filament-${FILAMENT_PLA}`)
    await user.click(within(row).getByRole('button', { name: /^Unarchive/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('exists')
    expect((await api.priceBook()).filaments.map((f) => f.id)).not.toContain(FILAMENT_PLA)
  })

  it('says what is wrong with a filament before sending it', async () => {
    const { user } = renderPage(<FilamentsEditor />)
    await screen.findByRole('table', { name: 'Filaments' })
    const form = screen.getByRole('form', { name: 'Add a filament' })
    await user.type(within(form).getByLabelText('Material'), 'PLA')
    await user.type(within(form).getByLabelText('Cost per kg'), 'cheap')
    await user.click(within(form).getByRole('button', { name: 'Add filament' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('cost per kilogram')
  })

  // #4253 §8.3 — on click only, and only with Bambuddy linked.
  it('imports the filaments from Bambuddy spools', async () => {
    const { user } = renderPage(<FilamentsEditor />)
    await screen.findByRole('table', { name: 'Filaments' })
    await user.click(await screen.findByRole('button', { name: 'Import from spools' }))
    const status = await screen.findByRole('status', { name: 'Spool import' })
    expect(status).toHaveTextContent('Bambu Lab PLA Basic Hot Pink: added at 15.91 per kg')
    expect(status).toHaveTextContent('Mistletoe Green: skipped, none of its spools has a price')
    expect(status).not.toHaveTextContent('Inland')
    await within(screen.getByRole('table', { name: 'Filaments' })).findByText(/Hot Pink/)
    expect((await api.priceBook()).filaments.find((f) => f.colour_name === 'Hot Pink')?.colour).toBe('#F5547C')
  })

  it('offers no import without Bambuddy', async () => {
    setMockSettings({ ...mockSettings(), bambuddy_url: null })
    renderPage(<FilamentsEditor />)
    await within(await screen.findByRole('table', { name: 'Filaments' })).findByText(/Sunflower Yellow/)
    expect(screen.queryByRole('button', { name: 'Import from spools' })).toBeNull()
    await expect(api.importSpools()).rejects.toThrow(/not linked/)
  })
})
