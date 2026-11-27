import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { printRunPoll } from '../../api/client'
import type { PrinterRow } from '../../api/types'
import {
  latchPrinterCommands,
  printerCommandKeys,
  printerCommandsPublished,
  setPrinterCommandSlow,
} from '../../mocks/features/printerCommands'
import {
  mockKProfileNotes,
  mockKProfiles,
  setKProfileExtruders,
  setKProfileUnconfirmed,
} from '../../mocks/features/printerKProfiles'
import { initialPrinters, setLanLinked } from '../../mocks/features/printers'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { said } from '../../test/said'
import { renderPage } from '../../test/utils'
import { flowOf, noteKeys, PrinterKProfiles, shownK } from './PrinterKProfiles'

const [initialH2c, garage] = initialPrinters() as [PrinterRow, PrinterRow]
// #7276: the K-profiles are read over ScadBuddy's LAN link, so printer 1 is linked over the LAN.
let h2c: PrinterRow
let single: PrinterRow
const poll = { ...printRunPoll }

beforeEach(() => {
  h2c = setLanLinked(initialH2c.id)
  single = { ...h2c, bambuddy_view: { ...h2c.bambuddy_view!, nozzle_count: 1 } }
})

afterEach(() => {
  Object.assign(printRunPoll, poll)
})

async function opened(row: PrinterRow = h2c) {
  const page = renderPage(<PrinterKProfiles row={row} />)
  await page.user.click(screen.getByRole('button', { name: 'K-profiles' }))
  return page
}

describe('PrinterKProfiles (#7154)', () => {
  it('reads nothing until it is opened', async () => {
    const reads: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.includes('/kprofiles')) reads.push(request.url)
    })
    const { user } = renderPage(<PrinterKProfiles row={h2c} />)
    expect(reads).toEqual([])
    await user.click(screen.getByRole('button', { name: 'K-profiles' }))
    await screen.findByRole('table', { name: 'K-profiles at 0.4 mm' })
    expect(reads.some((url) => url.includes('nozzle_diameter=0.4'))).toBe(true)
  })

  it('lists the K table with flow, K as Bambu Studio shows it, and each note', async () => {
    await opened()
    const pla = await screen.findByRole('row', { name: 'K-profile Bambu PLA Basic' })
    expect(pla).toHaveTextContent('GFA00')
    expect(pla).toHaveTextContent('Standard')
    expect(pla).toHaveTextContent('0.020')
    expect(pla).toHaveTextContent('Calibrated on the textured plate')
    const petg = screen.getByRole('row', { name: 'K-profile High Flow_Bambu PETG HF' })
    expect(petg).toHaveTextContent('High flow')
    expect(petg).toHaveTextContent('0.034')
    // A dual-nozzle printer shows the extruder.
    expect(screen.getByRole('columnheader', { name: 'Extruder' })).toBeInTheDocument()
  })

  it('a LAN-only dual printer shows the Extruder column (P54)', async () => {
    // The shape comes from the list's `extruders`, the printer's own report, not Bambuddy's view.
    const lanOnly = { ...h2c, bambuddy_printer_id: null, bambuddy_view: null }
    const { user } = await opened(lanOnly)
    expect(await screen.findByRole('columnheader', { name: 'Extruder' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add a K-profile' }))
    const form = screen.getByRole('form', { name: 'Add a K-profile' })
    await user.click(within(form).getByRole('combobox', { name: 'Extruder' }))
    expect(await screen.findByRole('option', { name: 'Both extruders' })).toBeInTheDocument()
  })

  it('reads one extruder from the list even where Bambuddy says two', async () => {
    setKProfileExtruders(1)
    await opened()
    await screen.findByRole('row', { name: 'K-profile Bambu PLA Basic' })
    expect(screen.queryByRole('columnheader', { name: 'Extruder' })).not.toBeInTheDocument()
  })

  it('takes a profile on extruder 1 for two extruders when the list names no count', async () => {
    setKProfileExtruders(null)
    await opened({ ...h2c, bambuddy_view: null })
    expect(await screen.findByRole('columnheader', { name: 'Extruder' })).toBeInTheDocument()
  })

  it('says when the printer holds no profile for the nozzle', async () => {
    const { user } = await opened()
    await screen.findByRole('row', { name: 'K-profile Bambu PLA Basic' })
    await choose(user, screen.getByRole('combobox', { name: 'Nozzle diameter' }), '0.6 mm')
    expect(await screen.findByText(/holds no K-profile for a 0.6 mm nozzle/)).toBeInTheDocument()
  })

  it("shows the route's refusal for a printer it cannot reach", async () => {
    await opened(garage)
    expect(await screen.findByRole('alert')).toHaveTextContent('Garage is not linked over the LAN')
  })

  it('adds a profile to both extruders of a dual-nozzle printer in one batch, with its note', async () => {
    const sent: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/kprofiles/batch')) sent.push(await request.clone().json())
    })
    const { user } = await opened()
    await screen.findByRole('row', { name: 'K-profile Bambu PLA Basic' })
    await user.click(screen.getByRole('button', { name: 'Add a K-profile' }))
    const form = screen.getByRole('form', { name: 'Add a K-profile' })
    await user.type(within(form).getByLabelText('Name'), 'Silk PLA')
    await user.type(within(form).getByLabelText('Filament id'), 'GFL96')
    await user.clear(within(form).getByLabelText('K value'))
    await user.type(within(form).getByLabelText('K value'), '0.018')
    await user.type(within(form).getByLabelText('Note'), 'slow walls')
    await user.click(within(form).getByRole('button', { name: 'Add K-profile' }))

    await said('Added Silk PLA.')
    expect(sent).toEqual([
      {
        profiles: [0, 1].map((extruder_id) => ({
          slot_id: 0,
          extruder_id,
          nozzle_id: 'HS00-0.4',
          nozzle_diameter: '0.4',
          filament_id: 'GFL96',
          name: 'Silk PLA',
          k_value: '0.018',
          setting_id: null,
        })),
      },
    ])
    expect(mockKProfiles().filter((row) => row.name === 'Silk PLA')).toHaveLength(2)
    expect(mockKProfileNotes()['name_Silk PLA_GFL96']).toBe('slow walls')
    expect(await screen.findAllByRole('row', { name: 'K-profile Silk PLA' })).toHaveLength(2)
  })

  it('adds a high-flow profile on a single-nozzle printer with the one-profile route', async () => {
    setKProfileExtruders(1)
    const { user } = await opened(single)
    await screen.findByRole('row', { name: 'K-profile Bambu PLA Basic' })
    expect(screen.queryByRole('columnheader', { name: 'Extruder' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add a K-profile' }))
    const form = screen.getByRole('form', { name: 'Add a K-profile' })
    await user.type(within(form).getByLabelText('Name'), 'HF PLA')
    await user.type(within(form).getByLabelText('Filament id'), 'GFA00')
    await choose(user, within(form).getByRole('combobox', { name: 'Flow' }), 'High flow')
    await user.click(within(form).getByRole('button', { name: 'Add K-profile' }))
    await said()
    expect(mockKProfiles().find((row) => row.name === 'HF PLA')?.nozzle_id).toBe('HH00-0.4')
  })

  it("edits a profile in its slot and its note under the note's own key", async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit Bambu PLA Basic' }))
    const form = screen.getByRole('form', { name: 'Edit Bambu PLA Basic' })
    expect(within(form).getByLabelText('Filament id')).toBeDisabled()
    await user.clear(within(form).getByLabelText('K value'))
    await user.type(within(form).getByLabelText('K value'), '0.025')
    await user.clear(within(form).getByLabelText('Note'))
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))

    await said('Saved Bambu PLA Basic.')
    expect(mockKProfiles().find((row) => row.slot_id === 1)?.k_value).toBe('0.025000')
    // A cleared note is deleted, under the key it was read from.
    expect(mockKProfileNotes().PFUS1).toBeUndefined()
  })

  it('edits only the note without writing the profile, so K keeps all six places', async () => {
    const writes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method !== 'GET' && new URL(request.url).pathname.includes('/kprofiles')) writes.push(new URL(request.url).pathname)
    })
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit High Flow_Bambu PETG HF' }))
    const form = screen.getByRole('form', { name: 'Edit High Flow_Bambu PETG HF' })
    expect(within(form).getByLabelText('K value')).toHaveValue('0.0345')
    await user.type(within(form).getByLabelText('Note'), 'wet spool')
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))

    await said('Saved the note on High Flow_Bambu PETG HF.')
    expect(writes.every((path) => path.endsWith('/kprofiles/notes'))).toBe(true)
    expect(mockKProfiles().find((row) => row.slot_id === 2)?.k_value).toBe('0.034500')
    expect(mockKProfileNotes()['slot_2_GFG02_1']).toBe('wet spool')
  })

  it('sends a changed K with every place the person typed', async () => {
    const sent: unknown[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'POST' && new URL(request.url).pathname.endsWith('/kprofiles')) sent.push(await request.clone().json())
    })
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit High Flow_Bambu PETG HF' }))
    const form = screen.getByRole('form', { name: 'Edit High Flow_Bambu PETG HF' })
    await user.clear(within(form).getByLabelText('K value'))
    await user.type(within(form).getByLabelText('K value'), '0.034512')
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))
    await said()
    expect(sent).toEqual([expect.objectContaining({ slot_id: 2, k_value: '0.034512', nozzle_id: 'HH00-0.4' })])
  })

  it('deletes and edits a profile the printer reported with no nozzle id', async () => {
    const bare = { ...mockKProfiles()[0]!, nozzle_id: '', setting_id: '' }
    server.use(
      http.get('/api/v1/printers/:id/kprofiles', () => HttpResponse.json({ profiles: [bare], nozzle_diameter: '0.4' })),
    )
    const bodies: { path: string; body: unknown }[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method !== 'GET') bodies.push({ path: new URL(request.url).pathname, body: await request.clone().json() })
    })
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit Bambu PLA Basic' }))
    const form = screen.getByRole('form', { name: 'Edit Bambu PLA Basic' })
    await user.clear(within(form).getByLabelText('K value'))
    await user.type(within(form).getByLabelText('K value'), '0.03')
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))
    await said()
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Deleted Bambu PLA Basic.'))

    // The edit rebuilds the id as Bambuddy's page does; the delete names the row as reported.
    const set = bodies.find((one) => one.path.endsWith('/kprofiles'))?.body
    expect(set).toEqual(expect.objectContaining({ nozzle_id: 'HS00-0.4', setting_id: null }))
    const removed = bodies.find((one) => one.path.endsWith('/kprofiles/delete'))?.body
    expect(removed).toEqual(expect.objectContaining({ nozzle_id: '', setting_id: '' }))
  })

  it('closes the form and says so when the profile saved but its note did not', async () => {
    server.use(http.put('/api/v1/printers/:id/kprofiles/notes', () => HttpResponse.json({ detail: 'boom' }, { status: 500 })))
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit Bambu PLA Basic' }))
    const form = screen.getByRole('form', { name: 'Edit Bambu PLA Basic' })
    await user.clear(within(form).getByLabelText('K value'))
    await user.type(within(form).getByLabelText('K value'), '0.03')
    await user.type(within(form).getByLabelText('Note'), ' again')
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))

    await said('Saved Bambu PLA Basic, but not its note')
    expect(screen.queryByRole('form', { name: 'Edit Bambu PLA Basic' })).not.toBeInTheDocument()
  })

  it('deletes a profile only once confirmed', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    expect(mockKProfiles()).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await said('Deleted Bambu PLA Basic.')
    expect(mockKProfiles().map((row) => row.name)).toEqual(['High Flow_Bambu PETG HF'])
    await waitFor(() => expect(screen.queryByRole('row', { name: 'K-profile Bambu PLA Basic' })).not.toBeInTheDocument())
  })

  it('says when the printer did not confirm a K-profile write', async () => {
    setKProfileUnconfirmed(true)
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    const status = await said()
    expect(status).toHaveTextContent('Sent; the printer did not confirm it.')
    expect(status).not.toHaveTextContent('Deleted')
  })

  it("shows the printer's refusal as ScadBuddy words it", async () => {
    server.use(
      http.post('/api/v1/printers/:id/kprofiles', () =>
        HttpResponse.json(
          { title: 'Conflict', status: 409, detail: 'The printer refused the K-profile write', code: 'printer_refused' },
          { status: 409 },
        ),
      ),
    )
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit Bambu PLA Basic' }))
    const form = screen.getByRole('form', { name: 'Edit Bambu PLA Basic' })
    await user.clear(within(form).getByLabelText('K value'))
    await user.type(within(form).getByLabelText('K value'), '0.03')
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('The printer refused the K-profile write')
  })

  it('sends each write with an Idempotency-Key', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await said()
    // The reads (GET) carry none; the one delete carries one.
    expect(printerCommandKeys().filter(Boolean)).toEqual([expect.stringMatching(/^[0-9a-f]{32}$/)])
  })

  it('follows a slow read and a slow write to their answers, saying the write was sent meanwhile', async () => {
    printRunPoll.intervalMs = 5
    const deletes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/kprofiles/delete')) deletes.push(request.method)
    })
    // The read stays at 202 until it is released: the table waits for it.
    let release = latchPrinterCommands()
    const { user } = await opened()
    await waitFor(() => expect(printerCommandKeys()).toHaveLength(1))
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    release()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    // So does the delete: it says it was sent, and nothing more, until the printer answers.
    release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    const status = await said('Sent to the printer. Waiting for it to answer.')
    expect(status).not.toHaveTextContent('Deleted')
    release()
    await said('Deleted Bambu PLA Basic.')
    expect(deletes).toEqual(['POST'])
  })

  it('keeps one empty live region from the start, so the first message is announced', async () => {
    const { user } = await opened()
    await screen.findByRole('table', { name: 'K-profiles at 0.4 mm' })
    const status = screen.getByRole('status')
    expect(status).toHaveAttribute('aria-live', 'polite')
    expect(status).toBeEmptyDOMElement()
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    // The same element, now holding the text: never a region added with its text in it.
    expect(await said('Sent to the printer.')).toBe(status)
    release()
  })

  it('sends the same key when a delete the page gave up on is pressed again, so the printer gets it once (P55)', async () => {
    printRunPoll.intervalMs = 1
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    printRunPoll.printerCommandFollowMs = 20
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await said('Do not send it again')
    release()
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await said('Deleted Bambu PLA Basic.')
    const [first, again] = printerCommandKeys().filter(Boolean)
    expect(first).toMatch(/^[0-9a-f]{32}$/)
    expect(again).toBe(first)
    expect(printerCommandsPublished()).toBe(1)
  })

  it('gives another command a new key after the page gave up on one (P55)', async () => {
    printRunPoll.intervalMs = 1
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    printRunPoll.printerCommandFollowMs = 20
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await said('Do not send it again')
    release()
    await user.click(screen.getByRole('button', { name: 'Delete High Flow_Bambu PETG HF' }))
    await user.click(screen.getByRole('button', { name: 'Delete High Flow_Bambu PETG HF' }))
    await said('Deleted High Flow_Bambu PETG HF.')
    const [first, other] = printerCommandKeys().filter(Boolean)
    expect(other).toMatch(/^[0-9a-f]{32}$/)
    expect(other).not.toBe(first)
    expect(printerCommandsPublished()).toBe(2)
  })

  it('stops following when the panel closes', async () => {
    printRunPoll.intervalMs = 5
    const follows: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.includes('/commands/')) follows.push(request.url)
    })
    const release = latchPrinterCommands()
    const { unmount } = await opened()
    await waitFor(() => expect(follows.length).toBeGreaterThan(1))
    unmount()
    const seen = follows.length
    await new Promise((resolve) => setTimeout(resolve, 60))
    expect(follows).toHaveLength(seen)
    release()
  })

  it("stops following the old nozzle's read when the nozzle changes", async () => {
    printRunPoll.intervalMs = 5
    const follows: string[] = []
    server.events.on('request:start', ({ request }) => {
      const path = new URL(request.url).pathname
      if (path.includes('/commands/')) follows.push(path.split('/').at(-1)!)
    })
    const release = latchPrinterCommands()
    const { user } = await opened()
    await waitFor(() => expect(follows.length).toBeGreaterThan(1))
    const [old] = follows
    await choose(user, screen.getByRole('combobox', { name: 'Nozzle diameter' }), '0.6 mm')
    await waitFor(() => expect(follows.some((id) => id !== old)).toBe(true))
    const seen = follows.filter((id) => id === old).length
    await new Promise((resolve) => setTimeout(resolve, 60))
    expect(follows.filter((id) => id === old)).toHaveLength(seen)
    release()
  })

  it('clears the waiting text when a nozzle change stops the follow of a delete', async () => {
    printRunPoll.intervalMs = 5
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    const release = latchPrinterCommands()
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    const status = await said('Sent to the printer. Waiting for it to answer.')
    await choose(user, screen.getByRole('combobox', { name: 'Nozzle diameter' }), '0.6 mm')
    await waitFor(() => expect(status).toBeEmptyDOMElement())
    release()
  })

  it('clears the waiting text when a nozzle change stops the follow of an edit', async () => {
    printRunPoll.intervalMs = 5
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit Bambu PLA Basic' }))
    const form = screen.getByRole('form', { name: 'Edit Bambu PLA Basic' })
    await user.clear(within(form).getByLabelText('K value'))
    await user.type(within(form).getByLabelText('K value'), '0.025')
    const release = latchPrinterCommands()
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))
    const status = await said('Sent to the printer. Waiting for it to answer.')
    await choose(user, screen.getByRole('combobox', { name: 'Nozzle diameter' }), '0.6 mm')
    await waitFor(() => expect(status).toBeEmptyDOMElement())
    release()
  })

  it('says not to send it again when the printer has not answered in time', async () => {
    printRunPoll.intervalMs = 1
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    printRunPoll.printerCommandFollowMs = 20
    setPrinterCommandSlow(1_000)
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Do not send it again'))
    expect(screen.getByRole('status')).toHaveTextContent('has not answered yet')
    expect(screen.getByRole('status')).not.toHaveTextContent('Deleted')
  })

  it('never says Bambuddy', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit Bambu PLA Basic' }))
    const region = screen.getByRole('region', { name: `K-profiles of ${h2c.name}` })
    expect(region.textContent).not.toMatch(/Bambuddy/)
    expect(within(region).getByRole('form')).toHaveTextContent('Kept in ScadBuddy, not on the printer.')
  })

  it('shows a read that fails as an alert', async () => {
    server.use(
      http.get('/api/v1/printers/:id/kprofiles', () =>
        HttpResponse.json({ detail: 'The printer did not answer the K-profile request' }, { status: 504 }),
      ),
    )
    await opened()
    expect(await screen.findByRole('alert')).toHaveTextContent('did not answer')
  })
})

describe('K-profile helpers', () => {
  it("reads the flow type from nozzle_id, standard when there is none", () => {
    expect(flowOf('HH00-0.4')).toBe('HH00')
    expect(flowOf('HS00-0.6')).toBe('HS00')
    expect(flowOf('')).toBe('HS00')
  })

  it('truncates K to three places', () => {
    expect(shownK('0.034500')).toBe('0.034')
    expect(shownK('0.0209')).toBe('0.020')
  })

  it("gives Bambuddy's note keys in its order", () => {
    const [profile] = mockKProfiles()
    expect(noteKeys(profile!)).toEqual(['PFUS1', 'slot_1_GFA00_0', 'name_Bambu PLA Basic_GFA00'])
  })
})
