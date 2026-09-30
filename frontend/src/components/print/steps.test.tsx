import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { NozzleStep } from './NozzleStep'
import { PlateStep } from './PlateStep'
import { QualityStep } from './QualityStep'

const installed = [
  { size: '0.2', flow: 'standard' as const, count: 1 },
  { size: '0.4', flow: 'high_flow' as const, count: 2 },
]

// Spec 2026-09-27 §4.1: mixed nozzle sizes are ALWAYS a server-side error, so there is one size
// radiogroup for both sides; only flow (Standard/High Flow) is per-side. Both steps are
// Advanced-only (#768), so neither takes a mode.
describe('NozzleStep', () => {
  it('marks installed sizes and warns on one that is not', () => {
    render(
      <NozzleStep sizes={['0.2', '0.4', '0.6', '0.8']} installed={installed}
        value={[{ size: '0.8', flow: 'standard' }, { size: '0.8', flow: 'standard' }]} onChange={vi.fn()} />,
    )
    expect(screen.getByRole('radio', { name: /0\.2 mm.*installed/i })).toBeInTheDocument()
    expect(screen.getByText(/No 0\.8 mm nozzle is installed/i)).toBeInTheDocument()
  })

  it('sets one size for both sides', () => {
    const onChange = vi.fn()
    render(
      <NozzleStep sizes={['0.2', '0.4']} installed={installed}
        value={[{ size: '0.4', flow: 'standard' }, { size: '0.4', flow: 'standard' }]} onChange={onChange} />,
    )
    fireEvent.click(screen.getByRole('radio', { name: /0\.2 mm/i }))
    expect(onChange).toHaveBeenLastCalledWith([
      { size: '0.2', flow: 'standard' }, { size: '0.2', flow: 'standard' },
    ])
  })

  it('offers Standard/High Flow per side, with no per-side size, and says High Flow slices as Standard', () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <NozzleStep sizes={['0.2', '0.4']} installed={installed}
        value={[{ size: '0.4', flow: 'standard' }, { size: '0.4', flow: 'standard' }]} onChange={onChange} />,
    )
    fireEvent.click(screen.getByRole('radio', { name: /left.*high flow/i }))
    expect(onChange).toHaveBeenLastCalledWith([
      { size: '0.4', flow: 'high_flow' }, { size: '0.4', flow: 'standard' },
    ])
    expect(screen.queryByRole('combobox', { name: /nozzle size/i })).toBeNull()

    rerender(
      <NozzleStep sizes={['0.2', '0.4']} installed={installed}
        value={[{ size: '0.4', flow: 'high_flow' }, { size: '0.4', flow: 'standard' }]} onChange={onChange} />,
    )
    expect(
      screen.getByText(/Bambuddy slices this as Standard flow; High Flow presets aren't supported by Bambuddy yet\./),
    ).toBeInTheDocument()
  })
})

describe('QualityStep', () => {
  const tiers = [
    { tier: 'fine' as const, process_name: '0.08mm High Quality @BBL H2C 0.2 nozzle' },
    { tier: 'standard' as const, process_name: '0.10mm Standard @BBL H2C 0.2 nozzle' },
    { tier: 'draft' as const, process_name: '0.12mm Balanced Quality @BBL H2C 0.2 nozzle' },
  ]

  it('lists every process, preselecting the tier', () => {
    const onChange = vi.fn()
    render(<QualityStep size="0.2" tiers={tiers} tier="fine" processName={null}
      processes={tiers.map((t) => t.process_name)} onChange={onChange} />)
    const select = screen.getByLabelText(/process/i) as HTMLSelectElement
    expect(select.value).toBe('0.08mm High Quality @BBL H2C 0.2 nozzle')
    fireEvent.change(select, { target: { value: '0.10mm Standard @BBL H2C 0.2 nozzle' } })
    expect(onChange).toHaveBeenLastCalledWith({
      tier: null, processName: '0.10mm Standard @BBL H2C 0.2 nozzle',
    })
  })

  it('clears a process this size does not offer, falling back to the tier', () => {
    // Final review 6: a remembered process from another size, or one Bambuddy no
    // longer lists, must not be sent unseen.
    const onChange = vi.fn()
    render(<QualityStep size="0.2" tiers={tiers} tier={null}
      processName="0.20mm Standard @BBL H2C" processes={tiers.map((t) => t.process_name)}
      onChange={onChange} />)
    expect(onChange).toHaveBeenCalledWith({ tier: 'standard', processName: null })
  })

  it('leaves a listed process alone, and waits for the list before judging', () => {
    const onChange = vi.fn()
    const { rerender } = render(<QualityStep size="0.2" tiers={tiers} tier={null}
      processName="0.08mm High Quality @BBL H2C 0.2 nozzle" processes={[]} onChange={onChange} />)
    rerender(<QualityStep size="0.2" tiers={tiers} tier={null}
      processName="0.08mm High Quality @BBL H2C 0.2 nozzle"
      processes={tiers.map((t) => t.process_name)} onChange={onChange} />)
    expect(onChange).not.toHaveBeenCalled()
  })
})

describe('PlateStep', () => {
  it('names the last plate as a guess and reminds to swap when it differs', () => {
    render(<PlateStep bedTypes={['Textured PEI Plate', 'Cool Plate']} value="Cool Plate"
      lastBedType="Textured PEI Plate" printerName="H2C" onChange={vi.fn()} />)
    expect(screen.getByText(/Last print used: Textured PEI Plate/)).toBeInTheDocument()
    expect(screen.getByText(/Swap to Cool Plate before this starts/)).toBeInTheDocument()
  })

  it('falls back to "printer" when no printer is named yet, the way hardware.py does', () => {
    // Fix round 1, important #1: printerName can be null before a printer is chosen;
    // it must read as a sentence ("The printer's…"), never "The 's…".
    render(<PlateStep bedTypes={['Textured PEI Plate', 'Cool Plate']} value="Cool Plate"
      lastBedType="Textured PEI Plate" printerName={null} onChange={vi.fn()} />)
    expect(screen.getByText(/The printer's last print used Textured PEI Plate\./)).toBeInTheDocument()
  })
})
