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
      nozzle_diameter: '0.4',
      nozzle_type: 'HH01',
      high_flow: true,
      prints: 14,
      print_seconds: 151200,
      grams: 812.5,
      pending: 1,
      first_seen_at: '2026-10-01T12:00:00Z',
      last_used_at: new Date(Date.now() - 3 * 24 * 3600 * 1000).toISOString(),
    },
    {
      position: 4,
      nozzle_diameter: '0.2',
      nozzle_type: 'HS00',
      high_flow: false,
      prints: 0,
      print_seconds: 0,
      grams: 0,
      pending: 0,
      first_seen_at: null,
      last_used_at: null,
    },
  ],
}

describe('HotendUsagePanel (#1298)', () => {
  it('lists each rack hotend with its prints, print time and last use', async () => {
    server.use(
      http.get('/api/v1/print/printers/1/rack-usage', () => HttpResponse.json(RACK)),
      http.get('/api/v1/print/printers/2/rack-usage', () => HttpResponse.json({ printer_id: 2, hotends: [] })),
    )
    renderPage(<HotendUsagePanel targets={TARGETS} />)

    const table = await screen.findByRole('table', { name: 'Hotend usage for H2C' })
    const [, used, unused] = within(table).getAllByRole('row')
    expect(used).toHaveTextContent('0.4 mm High Flow')
    expect(used).toHaveTextContent('14 (+1 queued)')
    expect(used).toHaveTextContent('42h 0m')
    expect(used).toHaveTextContent('3 days ago')
    expect(unused).toHaveTextContent('0.2 mm Standard')
    expect(unused).toHaveTextContent('Never')
    // A printer with no rack gets no table.
    expect(screen.queryByText(/Hotend usage · P1S/)).not.toBeInTheDocument()
  })

  it("says so when a printer's usage cannot be read", async () => {
    server.use(
      http.get('/api/v1/print/printers/1/rack-usage', () =>
        HttpResponse.json(
          { type: 'database-unavailable', title: 'Service Unavailable', status: 503, detail: 'could not read the recorded hotend usage (OperationalError)' },
          { status: 503 },
        ),
      ),
      http.get('/api/v1/print/printers/2/rack-usage', () => HttpResponse.json({ printer_id: 2, hotends: [] })),
    )
    renderPage(<HotendUsagePanel targets={TARGETS} />)

    expect(await screen.findByRole('alert')).toHaveTextContent('could not read the recorded hotend usage')
  })
})
