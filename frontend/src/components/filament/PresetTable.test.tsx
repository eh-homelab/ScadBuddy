import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { EffectivePreset, PresetInput } from '../../api/filament'
import { PresetTable } from './PresetTable'
import { presetRowsError } from './presetRows'

const ROW: PresetInput = { printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C' }

function Harness({ start, inherited, typeKeys, spy }: { start: PresetInput[]; inherited?: EffectivePreset[]; typeKeys?: ReadonlySet<string>; spy?: (rows: PresetInput[]) => void }) {
  const [rows, setRows] = useState(start)
  return (
    <PresetTable
      rows={rows}
      models={['H2C', 'X1C']}
      inherited={inherited}
      typeKeys={typeKeys}
      onChange={(next) => {
        setRows(next)
        spy?.(next)
      }}
    />
  )
}

describe('PresetTable', () => {
  it('adds a row for a printer model and a nozzle', async () => {
    const user = userEvent.setup()
    const spy = vi.fn()
    render(<Harness start={[]} spy={spy} />)
    await user.click(screen.getByRole('button', { name: 'Add preset' }))
    await user.type(screen.getByLabelText('Printer model'), 'H2C')
    await user.clear(screen.getByLabelText('Nozzle'))
    await user.type(screen.getByLabelText('Nozzle'), '0.6')
    await user.type(screen.getByLabelText('Slicer filament'), 'GFA00')
    expect(spy).toHaveBeenLastCalledWith([{ printer_model: 'H2C', nozzle_diameter: '0.6', slicer_filament: 'GFA00', slicer_filament_name: null }])
  })

  it('refuses * as a nozzle', async () => {
    const user = userEvent.setup()
    render(<Harness start={[ROW]} />)
    const nozzle = screen.getByLabelText('Nozzle')
    await user.clear(nozzle)
    await user.type(nozzle, '*')
    expect(await screen.findByText('A nozzle is a diameter in mm, such as 0.4. "*" is not a nozzle.')).toBeInTheDocument()
    expect(nozzle).toHaveAttribute('aria-invalid', 'true')
    expect(presetRowsError([{ ...ROW, nozzle_diameter: '*' }])).toMatch(/not a nozzle/)
    expect(presetRowsError([ROW])).toBeNull()
  })

  it('removes an own row', async () => {
    const user = userEvent.setup()
    const spy = vi.fn()
    render(<Harness start={[ROW]} spy={spy} />)
    await user.click(screen.getByRole('button', { name: 'Remove preset H2C 0.4' }))
    expect(spy).toHaveBeenLastCalledWith([])
  })

  it('refuses two rows for one printer model and nozzle', () => {
    expect(presetRowsError([ROW, { ...ROW, slicer_filament: 'GFA01' }])).toMatch(/H2C 0\.4/)
  })

  describe('inherited', () => {
    const FROM_TYPE: EffectivePreset = { printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C', overridden: false, migrated: false }

    it('shows the type row dimmed with From type, and overrides it on click', async () => {
      const user = userEvent.setup()
      const spy = vi.fn()
      render(<Harness start={[]} inherited={[FROM_TYPE]} spy={spy} />)
      const row = screen.getByRole('row', { name: /H2C 0\.4 from type/i })
      expect(within(row).getByText('From type')).toBeInTheDocument()
      expect(row).toHaveClass('opacity-60')
      await user.click(within(row).getByRole('button', { name: 'Override' }))
      expect(spy).toHaveBeenLastCalledWith([{ printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C' }])
    })

    it('shows an overridden row with Reset to type, which drops it', async () => {
      const user = userEvent.setup()
      const spy = vi.fn()
      render(<Harness start={[{ ...ROW, slicer_filament: 'GFA01' }]} inherited={[{ ...FROM_TYPE, slicer_filament: 'GFA01', overridden: true }]} typeKeys={new Set(['H2C|0.4'])} spy={spy} />)
      expect(screen.queryByText('From type')).toBeNull()
      await user.click(screen.getByRole('button', { name: 'Reset preset 1 to type' }))
      expect(spy).toHaveBeenLastCalledWith([])
    })

    it('offers Remove, not Reset to type, for an own row the type has no preset for', async () => {
      const user = userEvent.setup()
      const spy = vi.fn()
      // As the server answers: the type's 0.4 row and the spool's own 0.6 row (overridden: true).
      render(<Harness start={[{ ...ROW, nozzle_diameter: '0.6' }]} inherited={[FROM_TYPE, { ...FROM_TYPE, nozzle_diameter: '0.6', overridden: true }]} typeKeys={new Set(['H2C|0.4'])} spy={spy} />)
      expect(screen.queryByRole('button', { name: /to type/ })).toBeNull()
      await user.click(screen.getByRole('button', { name: 'Remove preset H2C 0.6' }))
      expect(spy).toHaveBeenLastCalledWith([])
    })
  })
})
