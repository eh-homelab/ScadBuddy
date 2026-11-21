import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import type { EffectiveKValue, TypeKValue, TypeKValueInput } from '../../api/filament'
import { KValueTable } from './KValueTable'
import { kValueRowsError } from './kValueRows'

const PRINTERS = [{ id: 'p1', name: 'Garage H2C' }]
const ROW: TypeKValueInput = { printer_id: 'p1', extruder: 0, nozzle_diameter: '0.4', nozzle_type: null, k_value: '0.020', name: null, cali_idx: 3 }
/** `row` as the API answers it once saved, set by `source`. */
const saving = (row: TypeKValueInput, source: TypeKValue['source']): TypeKValue => ({
  name: null,
  nozzle_type: null,
  cali_idx: null,
  setting_id: null,
  ...row,
  k_value: String(row.k_value),
  type_id: 't1',
  source,
})

function Harness({ start, inherited, typeKeys, spy }: { start: TypeKValueInput[]; inherited?: EffectiveKValue[]; typeKeys?: ReadonlySet<string>; spy?: (rows: TypeKValueInput[]) => void }) {
  const [rows, setRows] = useState(start)
  return (
    <KValueTable
      rows={rows}
      printers={PRINTERS}
      inherited={inherited}
      typeKeys={typeKeys}
      onChange={(next) => {
        setRows(next)
        spy?.(next)
      }}
    />
  )
}

describe('KValueTable', () => {
  it('says who set a saved row, and nothing for a person (#6856)', () => {
    const saved = new Map([
      ['p1|0|0.4', saving(ROW, 'printer_calibration')],
      ['p1|1|0.4', saving({ ...ROW, extruder: 1 }, 'person')],
    ])
    render(<KValueTable rows={[{ ...ROW, k_value: '0.02' }, { ...ROW, extruder: 1 }]} printers={PRINTERS} saved={saved} onChange={() => {}} />)
    expect(within(screen.getByRole('row', { name: 'K value 1' })).getByText('Printer calibration')).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: 'K value 2' })).queryByText(/calibration|From/)).toBeNull()
  })

  it('drops the source of a row edited and not yet saved (#6859)', async () => {
    const user = userEvent.setup()
    const saved = new Map([['p1|0|0.4', saving(ROW, 'printer_calibration')]])
    function Edited() {
      const [rows, setRows] = useState([ROW])
      return <KValueTable rows={rows} printers={PRINTERS} saved={saved} onChange={setRows} />
    }
    render(<Edited />)
    const row = screen.getByRole('row', { name: 'K value 1' })
    expect(within(row).getByText('Printer calibration')).toBeInTheDocument()
    const k = within(row).getByRole('textbox', { name: 'K value' })
    await user.clear(k)
    await user.type(k, '0.05')
    expect(within(row).queryByText('Printer calibration')).toBeNull()
    await user.clear(k)
    await user.type(k, '0.020')
    expect(within(row).getByText('Printer calibration')).toBeInTheDocument()
  })

  it('compares K as the server does: by its decimal digits, not as a float (#6857)', () => {
    const saved = new Map([['p1|0|0.4', saving({ ...ROW, k_value: '0.0200' }, 'printer_calibration')]])
    const shown = (k: string) => {
      const { unmount } = render(<KValueTable rows={[{ ...ROW, k_value: k }]} printers={PRINTERS} saved={saved} onChange={() => {}} />)
      const said = screen.queryByText('Printer calibration') !== null
      unmount()
      return said
    }
    expect(shown('0.02')).toBe(true)
    expect(shown('00.020')).toBe(true)
    // A float rounds this to 0.02; the server's Decimal does not, so a save makes it a person's.
    expect(shown('0.02000000000000000001')).toBe(false)
  })

  it('adds a row for the first printer', async () => {
    const user = userEvent.setup()
    const spy = vi.fn()
    render(<Harness start={[]} spy={spy} />)
    await user.click(screen.getByRole('button', { name: 'Add K value' }))
    expect(spy).toHaveBeenLastCalledWith([{ printer_id: 'p1', extruder: 0, nozzle_diameter: '0.4', nozzle_type: null, k_value: '', name: null }])
  })

  it('removes a row and edits the K value, keeping the calibration index', async () => {
    const user = userEvent.setup()
    const spy = vi.fn()
    render(<Harness start={[ROW, { ...ROW, extruder: 1 }]} spy={spy} />)
    const first = screen.getByRole('row', { name: 'K value 1' })
    await user.type(within(first).getByLabelText('K value'), '5')
    expect(spy).toHaveBeenLastCalledWith([{ ...ROW, k_value: '0.0205' }, { ...ROW, extruder: 1 }])
    await user.click(screen.getByRole('button', { name: 'Remove K value 2' }))
    expect(spy).toHaveBeenLastCalledWith([{ ...ROW, k_value: '0.0205' }])
  })

  it('refuses a duplicate key and a non-number', () => {
    expect(kValueRowsError([ROW, ROW])).toMatch(/same printer/)
    expect(kValueRowsError([{ ...ROW, k_value: 'abc' }])).toMatch(/number/)
    expect(kValueRowsError([ROW])).toBeNull()
  })

  describe('inherited', () => {
    const FROM_TYPE: EffectiveKValue = { printer_id: 'p1', extruder: 0, nozzle_diameter: '0.4', nozzle_type: 'hardened_steel', k_value: '0.020', name: 'Jade White 0.4', cali_idx: 3, setting_id: null, overridden: false }

    it('shows the type value dimmed with From type, and overrides it on click keeping its calibration', async () => {
      const user = userEvent.setup()
      const spy = vi.fn()
      render(<Harness start={[]} inherited={[FROM_TYPE]} spy={spy} />)
      const row = screen.getByRole('row', { name: /Garage H2C 0\.4 from type/i })
      expect(within(row).getByText('From type')).toBeInTheDocument()
      expect(row).toHaveClass('opacity-60')
      await user.click(within(row).getByRole('button', { name: 'Override Garage H2C 0.4' }))
      expect(spy).toHaveBeenLastCalledWith([{ printer_id: 'p1', extruder: 0, nozzle_diameter: '0.4', nozzle_type: 'hardened_steel', k_value: '0.020', name: 'Jade White 0.4', cali_idx: 3, setting_id: null }])
    })

    it('shows an overridden row with Reset to type, which drops it', async () => {
      const user = userEvent.setup()
      const spy = vi.fn()
      render(<Harness start={[ROW]} inherited={[{ ...FROM_TYPE, overridden: true }]} typeKeys={new Set(['p1|0|0.4'])} spy={spy} />)
      expect(screen.queryByText('From type')).toBeNull()
      await user.click(screen.getByRole('button', { name: 'Reset K value 1 to type' }))
      expect(spy).toHaveBeenLastCalledWith([])
    })

    it('offers Remove, not Reset to type, for an own row the type has no value for', async () => {
      const user = userEvent.setup()
      const spy = vi.fn()
      // As the server answers: the type's row, and the spool's own row (overridden: true) for a key the type lacks.
      render(<Harness start={[{ ...ROW, extruder: 1 }]} inherited={[FROM_TYPE, { ...FROM_TYPE, extruder: 1, overridden: true }]} typeKeys={new Set(['p1|0|0.4'])} spy={spy} />)
      expect(screen.queryByRole('button', { name: /to type/ })).toBeNull()
      await user.click(screen.getByRole('button', { name: 'Remove K value 1' }))
      expect(spy).toHaveBeenLastCalledWith([])
    })
  })
})
