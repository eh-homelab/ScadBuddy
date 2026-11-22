import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { FilamentInventoryView } from '../../api/filament'
import { renderPage } from '../../test/utils'
import { CheckTray, trayChecks } from './CheckTray'

const tray = (over: Partial<FilamentInventoryView['trays'][number]['trays'][number]>) => ({
  ams_id: 128,
  tray_id: 0,
  label: 'HT-A',
  material: 'PETG',
  rgba: '#A7AEB2FF',
  spool_id: 'spool-76',
  spool_number: 76,
  found_by: 'link' as const,
  mismatch: null,
  ...over,
})

describe('check this tray (#6713)', () => {
  it('finds each spool whose tray reports something else', () => {
    const view: FilamentInventoryView = {
      spools: [],
      trays: [
        {
          printer_id: 'p1',
          printer_name: 'H2C',
          trays: [
            tray({ mismatch: { reported_type: 'PETG', reported_rgba: '#A7AEB2FF' } }),
            tray({ ams_id: 0, label: 'AMS-A slot 1', spool_id: 'spool-4', spool_number: 4 }),
            tray({ ams_id: 0, tray_id: 1, spool_id: null, mismatch: { reported_type: 'PLA', reported_rgba: null } }),
          ],
        },
      ],
    }
    const checks = trayChecks(view)
    expect([...checks.keys()]).toEqual(['spool-76'])
    expect(checks.get('spool-76')).toEqual({ place: 'H2C HT-A', reportedType: 'PETG', reportedRgba: '#A7AEB2FF' })
  })

  it('names what the printer reports, and links the printer to the printers list (#6869)', () => {
    renderPage(<CheckTray check={{ place: 'H2C HT-A', reportedType: 'PETG', reportedRgba: '#A7AEB2FF' }} />)
    expect(screen.getByText('Check this tray')).toBeInTheDocument()
    const printer = screen.getByRole('link', { name: 'H2C HT-A' })
    expect(printer).toHaveAttribute('href', '/settings/printing#printers')
    expect(printer.parentElement).toHaveTextContent('H2C HT-A reports PETG #A7AEB2')
  })

  it('shows nothing without a check', () => {
    const { container } = render(<CheckTray />)
    expect(container).toBeEmptyDOMElement()
  })
})
