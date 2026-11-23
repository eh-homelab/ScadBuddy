import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { PrinterRow } from '../../api/types'
import {
  mockKProfileNotes,
  mockKProfiles,
  setKProfileAdminOnly,
  setKProfileUnconfirmed,
} from '../../mocks/features/printerKProfiles'
import { initialPrinters } from '../../mocks/features/printers'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { flowOf, noteKeys, PrinterKProfiles, shownK } from './PrinterKProfiles'

const [h2c, garage] = initialPrinters() as [PrinterRow, PrinterRow]
const single: PrinterRow = { ...h2c, bambuddy_view: { ...h2c.bambuddy_view!, nozzle_count: 1 } }

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

  it('says when the printer holds no profile for the nozzle', async () => {
    const { user } = await opened()
    await screen.findByRole('row', { name: 'K-profile Bambu PLA Basic' })
    await choose(user, screen.getByRole('combobox', { name: 'Nozzle diameter' }), '0.6 mm')
    expect(await screen.findByText(/holds no K-profile for a 0.6 mm nozzle/)).toBeInTheDocument()
  })

  it("shows the route's refusal for a printer it cannot reach", async () => {
    await opened(garage)
    expect(await screen.findByRole('alert')).toHaveTextContent('not linked to a Bambuddy printer')
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

    expect(await screen.findByRole('status')).toHaveTextContent('Added Silk PLA.')
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
    const { user } = await opened(single)
    await screen.findByRole('row', { name: 'K-profile Bambu PLA Basic' })
    expect(screen.queryByRole('columnheader', { name: 'Extruder' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add a K-profile' }))
    const form = screen.getByRole('form', { name: 'Add a K-profile' })
    await user.type(within(form).getByLabelText('Name'), 'HF PLA')
    await user.type(within(form).getByLabelText('Filament id'), 'GFA00')
    await choose(user, within(form).getByRole('combobox', { name: 'Flow' }), 'High flow')
    await user.click(within(form).getByRole('button', { name: 'Add K-profile' }))
    await screen.findByRole('status')
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

    expect(await screen.findByRole('status')).toHaveTextContent('Saved Bambu PLA Basic.')
    expect(mockKProfiles().find((row) => row.slot_id === 1)?.k_value).toBe('0.025000')
    // A cleared note is deleted, under the key it was read from.
    expect(mockKProfileNotes().PFUS1).toBeUndefined()
  })

  it('deletes a profile only once confirmed', async () => {
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    expect(mockKProfiles()).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Deleted Bambu PLA Basic.')
    expect(mockKProfiles().map((row) => row.name)).toEqual(['High Flow_Bambu PETG HF'])
    await waitFor(() => expect(screen.queryByRole('row', { name: 'K-profile Bambu PLA Basic' })).not.toBeInTheDocument())
  })

  it('never says saved when the printer did not confirm the write', async () => {
    setKProfileUnconfirmed(true)
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Delete Bambu PLA Basic' }))
    await user.click(screen.getByRole('button', { name: 'Delete Bambu PLA Basic' }))
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('the printer did not confirm it')
    expect(status).not.toHaveTextContent('Deleted')
  })

  it("names Bambuddy's admin-only scope when its auth refuses the write", async () => {
    setKProfileAdminOnly(true)
    const { user } = await opened()
    await user.click(await screen.findByRole('button', { name: 'Edit Bambu PLA Basic' }))
    const form = screen.getByRole('form', { name: 'Edit Bambu PLA Basic' })
    await user.click(within(form).getByRole('button', { name: 'Save K-profile' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent("No Bambuddy API key can change a printer's K-profiles")
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
