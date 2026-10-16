import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BambuddyTargets, PrinterRackUsage } from '../../api/types'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { HotendUsagePanel } from './HotendUsagePanel'

const TARGETS = { printers: [{ id: 1, name: 'H2C' }, { id: 2, name: 'P1S' }] } as BambuddyTargets

const RACK: PrinterRackUsage = {
  printer_id: 1,
  hotends: [
    {
      position: 2,
      serial: 'TEST-HOTEND-17',
      nozzle_diameter: '0.4',
      nozzle_type: 'HH01',
      high_flow: true,
      wear: 12,
      filament_id: 'GFA00',
      filament_name: 'Bambu PLA Basic',
      filament_material: 'PLA',
      filament_colour: '#3F8E43',
      prints: 14,
      print_seconds: 151200,
      grams: 812.5,
      pending: 1,
      first_seen_at: '2026-10-01T12:00:00Z',
      last_used_at: new Date(Date.now() - 3 * 24 * 3600 * 1000).toISOString(),
      spools: [
        { spool_id: 7, label: 'Mistletoe Green PLA', material: 'PLA', colour: '#3F8E43', prints: 3, grams: 120.4, last_used_at: null },
        { spool_id: 8, label: 'Inland Black PLA', material: 'PLA', colour: '#000000', prints: 1, grams: 0, last_used_at: null },
      ],
    },
    {
      position: 4,
      serial: null,
      nozzle_diameter: '0.2',
      nozzle_type: 'HS00',
      high_flow: false,
      wear: 128,
      filament_id: null,
      filament_name: null,
      filament_material: null,
      filament_colour: null,
      prints: 0,
      print_seconds: 0,
      grams: 0,
      pending: 0,
      first_seen_at: null,
      last_used_at: null,
      spools: [],
    },
  ],
}

describe('HotendUsagePanel (#1298, #2170)', () => {
  it('lists each rack hotend with its serial, wear, loaded filament, usage and spools', async () => {
    server.use(
      http.get('/api/v1/print/printers/1/rack-usage', () => HttpResponse.json(RACK)),
      http.get('/api/v1/print/printers/2/rack-usage', () => HttpResponse.json({ printer_id: 2, hotends: [] })),
    )
    renderPage(<HotendUsagePanel targets={TARGETS} />)

    const rack = await screen.findByRole('region', { name: 'Hotend usage for H2C' })
    const used = within(rack).getByRole('listitem', { name: 'Position 2' })
    expect(used).toHaveTextContent('0.4 mm High Flow')
    expect(used).toHaveTextContent('Serial TEST-HOTEND-17')
    expect(used).toHaveTextContent('Wear12%')
    expect(within(used).getByRole('img', { name: 'Bambu PLA Basic #3F8E43' })).toBeInTheDocument()
    expect(used).toHaveTextContent('14 (+1 queued)')
    expect(used).toHaveTextContent('42h 0m')
    expect(used).toHaveTextContent('3 days ago')
    const spools = within(used).getAllByRole('listitem')
    expect(spools.map((spool) => spool.textContent)).toEqual(['Mistletoe Green PLA×3 · 120 g', 'Inland Black PLA×1'])

    const unused = within(rack).getByRole('listitem', { name: 'Position 4' })
    expect(unused).toHaveTextContent('0.2 mm Standard')
    expect(unused).toHaveTextContent('No serial reported')
    expect(unused).toHaveTextContent('Nothing loaded')
    expect(unused).toHaveTextContent('Wearnot reported')
    expect(unused).toHaveTextContent('Never')
    expect(within(unused).queryByRole('list', { name: /Spools run through/ })).not.toBeInTheDocument()
    // A printer with no rack gets nothing.
    expect(screen.queryByText(/P1S/)).not.toBeInTheDocument()
  })

  it('names a failed read in one quiet note, not an alert (#2102)', async () => {
    server.use(
      http.get('/api/v1/print/printers/1/rack-usage', () => HttpResponse.json(RACK)),
      http.get('/api/v1/print/printers/2/rack-usage', () =>
        HttpResponse.json(
          { type: 'bambuddy-unreachable', title: 'Bad Gateway', status: 502, detail: 'the printer is offline' },
          { status: 502 },
        ),
      ),
    )
    renderPage(<HotendUsagePanel targets={TARGETS} />)

    expect(await screen.findByText(/Hotend usage could not be read for P1S \(the printer is offline\)/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Hotend usage for H2C' })).toBeInTheDocument()
  })
})
