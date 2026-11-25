import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { PrinterRow } from '../../api/types'
import { amsPresetsSaved, amsSent, setAmsAdminOnly, setAmsPrinting, setAmsTrayNow } from '../../mocks/features/printerAmsSlots'
import { initialPrinters } from '../../mocks/features/printers'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { PrinterAmsSlots } from './PrinterAmsSlots'

const [h2c, garage] = initialPrinters() as [PrinterRow, PrinterRow]

async function opened(row: PrinterRow = h2c) {
  const page = renderPage(<PrinterAmsSlots row={row} />)
  await page.user.click(screen.getByRole('button', { name: 'AMS slots' }))
  return page
}

describe('PrinterAmsSlots (#7153)', () => {
  it('reads nothing until it is opened', async () => {
    const reads: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/ams')) reads.push(request.url)
    })
    const { user } = renderPage(<PrinterAmsSlots row={h2c} />)
    expect(reads).toEqual([])
    await user.click(screen.getByRole('button', { name: 'AMS slots' }))
    await screen.findByRole('button', { name: 'AMS-A slot 1' })
    expect(reads).toHaveLength(1)
  })

  it('shows each slot with its material, preset, saved preset and the one feeding', async () => {
    await opened()
    const first = await screen.findByRole('button', { name: 'AMS-A slot 1' })
    expect(first).toHaveTextContent('Feeding the hotend')
    const second = screen.getByRole('button', { name: 'AMS-A slot 2' })
    expect(second).toHaveTextContent('PETG')
    expect(second).toHaveTextContent('Saved: Bambu PETG Basic @BBL H2C')
    expect(screen.getByRole('button', { name: 'AMS-A slot 4' })).toHaveTextContent('Empty')
  })

  it('asks before it loads a slot, and sends nothing on Cancel', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    await user.click(screen.getByRole('button', { name: 'Load' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(`Load AMS-A slot 2 on ${h2c.name}?`)
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(amsSent()).toEqual([])

    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    await waitFor(() => expect(amsSent()).toEqual(['load 0/1']))
    expect(await screen.findByRole('status')).toHaveTextContent('Load: sent to AMS-A slot 2')
  })

  it('cannot load an empty slot', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 4' }))
    expect(screen.getByRole('button', { name: 'Load' })).toBeDisabled()
  })

  it('turns every printer write off while the printer prints', async () => {
    setAmsPrinting(true)
    const { user } = await opened()
    expect(await screen.findByText(/is printing\. Slot changes wait/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'AMS-A slot 2' }))
    const actions = screen.getByRole('group', { name: 'Actions for AMS-A slot 2' })
    for (const name of ['Configure…', 'Load', 'Unload', 'Read the tag again', 'Reset']) {
      expect(within(actions).getByRole('button', { name })).toBeDisabled()
    }
  })

  it('configures a slot by hand after it asks, and saves the preset it named', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 4' }))
    await user.click(screen.getByRole('button', { name: 'Configure…' }))
    const form = screen.getByRole('form', { name: 'Configure AMS-A slot 4' })
    await user.type(within(form).getByLabelText('Material'), 'PETG')
    await user.type(within(form).getByLabelText('Preset id'), 'GFG99')
    await user.type(within(form).getByLabelText('Preset name'), 'Generic PETG')
    await user.type(within(form).getByLabelText('Nozzle min (°C)'), '230')
    await user.type(within(form).getByLabelText('Nozzle max (°C)'), '260')
    await user.click(within(form).getByRole('button', { name: 'Configure AMS-A slot 4…' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent(`Configure AMS-A slot 4 on ${h2c.name}?`)
    await user.click(within(dialog).getByRole('button', { name: 'Configure' }))
    await waitFor(() => expect(amsSent()).toEqual(['configure 0/3']))
    const slot = await screen.findByRole('button', { name: 'AMS-A slot 4' })
    await waitFor(() => expect(slot).toHaveTextContent('Saved: Generic PETG'))
  })

  async function configureForm(user: Awaited<ReturnType<typeof opened>>['user'], name: string) {
    await user.click(await screen.findByRole('button', { name }))
    await user.click(screen.getByRole('button', { name: 'Configure…' }))
    return screen.getByRole('form', { name: `Configure ${name}` })
  }

  async function confirm(user: Awaited<ReturnType<typeof opened>>['user'], form: HTMLElement, name: string) {
    await user.click(within(form).getByRole('button', { name: `Configure ${name}…` }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Configure' }))
  }

  it('saves the preset with the source the form names', async () => {
    const { user } = await opened()
    const form = await configureForm(user, 'AMS-A slot 4')
    await user.type(within(form).getByLabelText('Material'), 'PETG')
    await user.type(within(form).getByLabelText('Preset id'), 'PFUS1234')
    await user.type(within(form).getByLabelText('Preset name'), 'My PETG')
    await user.type(within(form).getByLabelText('Nozzle min (°C)'), '230')
    await user.type(within(form).getByLabelText('Nozzle max (°C)'), '260')
    await choose(user, within(form).getByRole('combobox', { name: 'Preset source' }), 'Local')
    await confirm(user, form, 'AMS-A slot 4')
    await waitFor(() =>
      expect(amsPresetsSaved()).toEqual([
        { preset_id: 'PFUS1234', preset_name: 'My PETG', preset_source: 'local', tray_info_idx: 'PFUS1234' },
      ]),
    )
  })

  it('saves a spool’s ScadBuddy preset with the source the route names', async () => {
    const { user } = await opened()
    const form = await configureForm(user, 'AMS-A slot 4')
    await choose(user, within(form).getByRole('combobox', { name: 'Preset source' }), 'Orca cloud')
    await user.click(within(form).getByRole('combobox', { name: 'From a spool' }))
    const [, first] = await screen.findAllByRole('option')
    await user.click(first!)
    // The spool's preset names its own source, so the form offers none.
    expect(within(form).queryByRole('combobox', { name: 'Preset source' })).not.toBeInTheDocument()
    await confirm(user, form, 'AMS-A slot 4')
    await waitFor(() =>
      expect(amsPresetsSaved()).toEqual([
        { preset_id: '141', preset_name: 'Shop PLA', preset_source: 'local', tray_info_idx: '141' },
      ]),
    )
    const slot = await screen.findByRole('button', { name: 'AMS-A slot 4' })
    await waitFor(() => expect(slot).toHaveTextContent('Saved: Shop PLA'))
  })

  it('clears the saved preset when the configure sent none', async () => {
    const { user } = await opened()
    const form = await configureForm(user, 'AMS-A slot 2')
    await user.clear(within(form).getByLabelText('Preset id'))
    await confirm(user, form, 'AMS-A slot 2')
    await waitFor(() => expect(amsSent()).toEqual(['configure 0/1']))
    const slot = await screen.findByRole('button', { name: 'AMS-A slot 2' })
    await waitFor(() => expect(slot).not.toHaveTextContent('Saved:'))
    expect(amsPresetsSaved()).toEqual([])
  })

  it('warns when the slot was configured but its preset was not saved', async () => {
    setAmsAdminOnly(true)
    const { user } = await opened()
    const form = await configureForm(user, 'AMS-A slot 2')
    await confirm(user, form, 'AMS-A slot 2')
    const status = await screen.findByRole('status')
    await waitFor(() => expect(status).toHaveTextContent('The slot’s saved preset was not changed'))
    expect(status).toHaveClass('text-warn')
  })

  it('labels a unit, which names its place too', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    await user.type(screen.getByLabelText('Label for AMS-A'), 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save label' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Labelled AMS-A “Left rack”.')
    expect(await screen.findByRole('heading', { name: 'Left rack' })).toBeInTheDocument()
  })

  it('warns when the label was saved but its place was not named', async () => {
    server.use(
      http.put('/api/v1/printers/:id/ams/:ams/label', () =>
        HttpResponse.json({ ams_id: 0, label: 'Left rack', place: null, place_problem: 'no_printer' }),
      ),
    )
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    await user.type(screen.getByLabelText('Label for AMS-A'), 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save label' }))
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('Its place was not named: the printer was deleted meanwhile.')
    expect(status).toHaveClass('text-warn')
  })

  it('reads the tag again when the status names no tray, as the route allows', async () => {
    setAmsTrayNow(null)
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    expect(screen.getByRole('button', { name: 'Read the tag again' })).toBeEnabled()
  })

  it('does not read the tag again while a tray feeds the printer', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    expect(screen.getByRole('button', { name: 'Read the tag again' })).toBeDisabled()
  })

  it('starts Rename from the unit’s label each time it opens', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    await user.type(screen.getByLabelText('Label for AMS-A'), 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save label' }))
    await screen.findByRole('heading', { name: 'Left rack' })
    await user.click(screen.getByRole('button', { name: 'Rename AMS-A' }))
    expect(screen.getByLabelText('Label for AMS-A')).toHaveValue('Left rack')
    await user.click(screen.getByRole('button', { name: 'Clear label' }))
    await screen.findByRole('heading', { name: 'AMS-A' })
    await user.click(screen.getByRole('button', { name: 'Rename AMS-A' }))
    expect(screen.getByLabelText('Label for AMS-A')).toHaveValue('')
  })

  it("says why Bambuddy refused a label", async () => {
    setAmsAdminOnly(true)
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    await user.type(screen.getByLabelText('Label for AMS-A'), 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save label' }))
    expect(await screen.findByRole('status')).toHaveTextContent("while Bambuddy's auth is on")
  })

  it("shows the route's refusal for a printer it cannot reach", async () => {
    await opened(garage)
    expect(await screen.findByRole('alert')).toHaveTextContent('not linked to a Bambuddy printer')
  })
})
