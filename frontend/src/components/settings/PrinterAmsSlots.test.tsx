import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { printRunPoll } from '../../api/client'
import type { PrinterRow } from '../../api/types'
import {
  amsPresetsSaved,
  amsSent,
  setAmsPrinting,
  setAmsTrayNow,
  setAmsUnconfirmed,
} from '../../mocks/features/printerAmsSlots'
import {
  latchPrinterCommands,
  printerCommandKeys,
  printerCommandsPublished,
  setPrinterCommandSlow,
} from '../../mocks/features/printerCommands'
import { initialPrinters, setLanLinked } from '../../mocks/features/printers'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { said } from '../../test/said'
import { renderPage } from '../../test/utils'
import { PrinterAmsSlots } from './PrinterAmsSlots'

const [initialH2c, garage] = initialPrinters() as [PrinterRow, PrinterRow]
// #7276: the slots are read and written over ScadBuddy's LAN link, so printer 1 is linked over the LAN.
let h2c: PrinterRow
const poll = { ...printRunPoll }

beforeEach(() => {
  h2c = setLanLinked(initialH2c.id)
})

afterEach(() => {
  Object.assign(printRunPoll, poll)
})

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
    await said('Load: sent to AMS-A slot 2')
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

  it('saves the preset a configure from a spool sent', async () => {
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
        { preset_id: 'GFL99', preset_name: 'Shop PLA', preset_source: 'cloud', tray_info_idx: 'GFL99' },
      ]),
    )
    const slot = await screen.findByRole('button', { name: 'AMS-A slot 4' })
    await waitFor(() => expect(slot).toHaveTextContent('Saved: Shop PLA'))
  })

  it('saves the generic preset the slot was sent when none was typed', async () => {
    const { user } = await opened()
    const form = await configureForm(user, 'AMS-A slot 2')
    await user.clear(within(form).getByLabelText('Preset id'))
    await confirm(user, form, 'AMS-A slot 2')
    await waitFor(() => expect(amsSent()).toEqual(['configure 0/1']))
    await waitFor(() =>
      expect(amsPresetsSaved()).toEqual([
        { preset_id: 'GFG99', preset_name: 'Bambu PETG Basic', preset_source: 'cloud', tray_info_idx: 'GFG99' },
      ]),
    )
  })

  it('warns when the slot was configured but its preset was not saved', async () => {
    server.use(
      http.put('/api/v1/printers/:id/trays/:ams/:tray/preset', () =>
        HttpResponse.json({ title: 'Error', status: 500, detail: 'The preset could not be saved' }, { status: 500 }),
      ),
    )
    const { user } = await opened()
    const form = await configureForm(user, 'AMS-A slot 2')
    await confirm(user, form, 'AMS-A slot 2')
    const status = await said()
    await waitFor(() => expect(status).toHaveTextContent('The slot’s saved preset was not changed'))
    expect(status).toHaveClass('text-warn')
  })

  it('labels a unit, which names its place too', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    await user.type(screen.getByLabelText('Label for AMS-A'), 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save label' }))
    await said('Labelled AMS-A “Left rack”.')
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
    const status = await said()
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

  it('says why a label was not saved', async () => {
    server.use(
      http.put('/api/v1/printers/:id/ams/:ams/label', () =>
        HttpResponse.json({ title: 'Unprocessable', status: 422, detail: 'The label must hold printable characters only' }, { status: 422 }),
      ),
    )
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    await user.type(screen.getByLabelText('Label for AMS-A'), 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save label' }))
    await said('The label must hold printable characters only')
  })

  it('says the label names the unit’s place in ScadBuddy', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Rename AMS-A' }))
    expect(screen.getByRole('form', { name: 'Label AMS-A' })).toHaveTextContent('The label also names this unit’s place in ScadBuddy.')
  })

  it("shows the route's refusal for a printer it cannot reach", async () => {
    await opened(garage)
    expect(await screen.findByRole('alert')).toHaveTextContent('Garage is not linked over the LAN')
  })

  it('follows a slow configure to its answer, and saves the preset it sent (P44)', async () => {
    printRunPoll.intervalMs = 50
    const { user } = await opened()
    const form = await configureForm(user, 'AMS-A slot 2')
    setPrinterCommandSlow(2)
    await user.clear(within(form).getByLabelText('Preset id'))
    await user.type(within(form).getByLabelText('Preset id'), 'GFG99')
    await user.clear(within(form).getByLabelText('Preset name'))
    await user.type(within(form).getByLabelText('Preset name'), 'Generic PETG')
    await confirm(user, form, 'AMS-A slot 2')
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Configured AMS-A slot 2'), { timeout: 3000 })
    // The saved preset follows the configure's answer, never the 202's body.
    expect(amsPresetsSaved()).toEqual([
      { preset_id: 'GFG99', preset_name: 'Generic PETG', preset_source: 'cloud', tray_info_idx: 'GFG99' },
    ])
    expect(amsSent()).toEqual(['configure 0/1'])
  })

  it('says the command was sent while the printer has not answered, and not to send it again when it gives up', async () => {
    printRunPoll.intervalMs = 1
    printRunPoll.printerCommandFollowMs = 20
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    setPrinterCommandSlow(1_000)
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Do not send it again'))
    expect(amsSent()).toEqual(['load 0/1'])
  })

  it('sends the same key when a load the page gave up on is pressed again, so the printer gets it once (P55)', async () => {
    printRunPoll.intervalMs = 1
    printRunPoll.printerCommandFollowMs = 20
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    await said('Do not send it again')
    release()
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    await said('Load: sent to AMS-A slot 2.')
    const [first, again] = printerCommandKeys()
    expect(again).toBe(first)
    expect(printerCommandsPublished()).toBe(1)
    expect(amsSent()).toEqual(['load 0/1'])
  })

  it('gives another slot command a new key after the page gave up on one (P55)', async () => {
    printRunPoll.intervalMs = 1
    printRunPoll.printerCommandFollowMs = 20
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    await said('Do not send it again')
    release()
    await user.click(screen.getByRole('button', { name: 'Reset' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Reset' }))
    await said('Reset: sent to AMS-A slot 2.')
    const [first, other] = printerCommandKeys()
    expect(other).not.toBe(first)
    expect(printerCommandsPublished()).toBe(2)
  })

  it('stops following a slot command when the panel closes', async () => {
    printRunPoll.intervalMs = 5
    const follows: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.includes('/commands/')) follows.push(request.url)
    })
    const { user, unmount } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    await waitFor(() => expect(follows.length).toBeGreaterThan(1))
    unmount()
    const seen = follows.length
    await new Promise((resolve) => setTimeout(resolve, 60))
    expect(follows).toHaveLength(seen)
    release()
  })

  it('keeps one empty live region from the start, so the first message is announced', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    const status = screen.getByRole('status')
    expect(status).toHaveAttribute('aria-live', 'polite')
    expect(status).toBeEmptyDOMElement()
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    expect(await said('Sent to the printer.')).toBe(status)
    release()
    await said('Load: sent to AMS-A slot 2.')
  })

  it('says when the printer did not confirm a slot command', async () => {
    setAmsUnconfirmed(true)
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    const status = await said()
    expect(status).toHaveTextContent('Load: sent to AMS-A slot 2; the printer did not confirm it.')
    expect(status).toHaveClass('text-warn')
  })

  it("shows a refusal's own detail", async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    await user.click(screen.getByRole('button', { name: 'Unload' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Unload' }))
    await said('H2C is not loaded from AMS-A slot 2')
  })

  it('sends each slot command with an Idempotency-Key', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    await user.click(screen.getByRole('button', { name: 'Load' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Load' }))
    await said()
    expect(printerCommandKeys()).toEqual([expect.stringMatching(/^[0-9a-f]{32}$/)])
  })

  it('never says Bambuddy', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'AMS-A slot 2' }))
    await user.click(screen.getByRole('button', { name: 'Rename AMS-A' }))
    const region = screen.getByRole('region', { name: `AMS slots of ${h2c.name}` })
    expect(region.textContent).not.toMatch(/Bambuddy/)
    await user.click(screen.getByRole('button', { name: 'Load' }))
    expect((await screen.findByRole('alertdialog')).textContent).not.toMatch(/Bambuddy/)
  })

  it('says the printer has not reported lately, not Bambuddy', async () => {
    server.use(
      http.get('/api/v1/printers/:id/ams', () =>
        HttpResponse.json({
          printer_id: h2c.id,
          name: 'H2C',
          connected: false,
          state: null,
          printing: false,
          ams_busy: false,
          tray_now: null,
          units: [],
        }),
      ),
    )
    await opened()
    expect(await screen.findByText(/ScadBuddy has not heard from H2C lately/)).toBeInTheDocument()
  })
})
