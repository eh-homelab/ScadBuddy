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

  // #6136 — each density says where it came from; a filament added by hand takes its
  // spools', and "Fill densities" fills the ones born empty, row by row.
  it('shows where each density comes from and fills the empty ones', async () => {
    const linked = mockSettings()
    setMockSettings({ ...linked, bambuddy_url: null })
    const pink = await api.createFilament({ material: 'PLA', subtype: 'Basic', brand: 'Bambu Lab', colour_name: 'Hot Pink', colour: '#F5547C', cost_per_kg: '20', density_g_cm3: null })
    setMockSettings(linked)
    const none = await api.createFilament({ material: 'PLA', subtype: 'Tri Color', brand: 'Bambu Lab', colour_name: 'Neon City', colour: '#0047BB', cost_per_kg: '25', density_g_cm3: null })
    const { user } = renderPage(<FilamentsEditor />)
    expect(await within(await screen.findByTestId(`filament-${pink.id}`)).findByText('Not set: no estimate')).toBeInTheDocument()
    expect(within(screen.getByTestId(`filament-${FILAMENT_PLA}`)).getByText('1.24 g/cm³')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Fill densities' }))
    const status = await screen.findByRole('status', { name: 'Density fill' })
    expect(status).toHaveTextContent('Bambu Lab PLA Basic Hot Pink: filled, 1.26 g/cm³ from Bambu PLA Basic @base (spool preset GFA00)')
    expect(status).toHaveTextContent('Neon City: no source (Bambuddy links no preset or density to it); enter its density')
    await within(screen.getByTestId(`filament-${pink.id}`)).findByText('1.26 g/cm³, from Bambu PLA Basic @base (spool preset GFA00)')
    expect(within(screen.getByTestId(`filament-${none.id}`)).getByText('Not set: no estimate')).toBeInTheDocument()
  })

  it('fills a filament added by hand from its spools', async () => {
    const made = await api.createFilament({ material: 'PLA', subtype: 'Basic', brand: 'Bambu Lab', colour_name: 'Mistletoe Green', colour: '#3F8E43', cost_per_kg: '20', density_g_cm3: null })
    expect([made.density_g_cm3, made.density_profile]).toEqual(['1.26', 'Bambu PLA Basic @base (spool preset GFA00)'])
  })

  // #6209 — a clear filament is drawn over a checkerboard, with its 8-digit hex, never as
  // black; a colour set by hand says so, and can be handed back to the spool import.
  it('draws a clear filament as translucent and keeps a colour set by hand', async () => {
    const clear = await api.createFilament({ material: 'PETG', subtype: 'Translucent', brand: 'Bambu Lab', colour_name: 'Clear', colour: '#000000', alpha: 0, effect: 'translucent', cost_per_kg: '15.91', density_g_cm3: '1.25' })
    const { user } = renderPage(<FilamentsEditor />)
    const row = await screen.findByTestId(`filament-${clear.id}`)
    expect(within(row).getByText('#00000000')).toBeInTheDocument()
    expect(within(row).getByText('translucent, set by hand')).toBeInTheDocument()
    expect(within(row).getByTestId('filament-swatch').style.backgroundImage).toContain('repeating-conic-gradient')
    expect(within(screen.getByTestId(`filament-${FILAMENT_PLA}`)).queryByText(/[0-9A-F]{8}$/)).toBeNull()

    await user.click(within(row).getByRole('button', { name: 'Edit' }))
    const edit = screen.getByRole('form', { name: 'Edit filament' })
    expect(within(edit).getByLabelText('Colour')).toHaveValue('#00000000')
    await user.clear(within(edit).getByLabelText('Colour'))
    await user.type(within(edit).getByLabelText('Colour'), '#E3ECEF80')
    await user.click(within(edit).getByRole('button', { name: 'Save filament' }))
    await waitFor(async () => {
      const saved = (await api.priceBook()).filaments.find((f) => f.id === clear.id)
      expect([saved?.colour, saved?.alpha]).toEqual(['#E3ECEF', 128])
    })

    await user.click(within(screen.getByTestId(`filament-${clear.id}`)).getByRole('button', { name: /^Use the spool colour/ }))
    await waitFor(async () => expect((await api.priceBook()).filaments.find((f) => f.id === clear.id)?.colour_source).toBe('import'))
    expect(within(screen.getByTestId(`filament-${clear.id}`)).queryByRole('button', { name: /^Use the spool colour/ })).toBeNull()
  })

  it('offers no import without Bambuddy', async () => {
    setMockSettings({ ...mockSettings(), bambuddy_url: null })
    renderPage(<FilamentsEditor />)
    await within(await screen.findByRole('table', { name: 'Filaments' })).findByText(/Sunflower Yellow/)
    expect(screen.queryByRole('button', { name: 'Import from spools' })).toBeNull()
    await expect(api.importSpools()).rejects.toThrow(/not linked/)
  })
})
