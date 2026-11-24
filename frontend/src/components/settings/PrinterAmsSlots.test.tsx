import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PrinterRow } from '../../api/types'
import { amsSent, setAmsLabelAdminOnly, setAmsPrinting, setAmsTrayNow } from '../../mocks/features/printerAmsSlots'
import { initialPrinters } from '../../mocks/features/printers'
import { server } from '../../mocks/server'
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

  it('labels a unit, which names its place too', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    await user.type(screen.getByLabelText('Label for AMS-A'), 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save label' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Labelled AMS-A “Left rack”.')
    expect(await screen.findByRole('heading', { name: 'Left rack' })).toBeInTheDocument()
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
    setAmsLabelAdminOnly(true)
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
