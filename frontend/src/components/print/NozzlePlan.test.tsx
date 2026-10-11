import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { NozzlePlan } from '../../api/types'
import { NozzlePlanLine, NozzlePlanStep } from './NozzlePlan'

const plan: NozzlePlan = {
  slots: [
    { slot_id: 1, side: 'R', name: 'Mistletoe Green', colour: '#3F8E43', material: 'PLA', by_hand: false },
    { slot_id: 2, side: 'L', name: 'Inland Black', colour: '#1A1A1A', material: 'PLA', by_hand: false },
  ],
  size: '0.4',
  left_flow: 'high_flow',
  right_flow: 'high_flow',
  track_switch: true,
  summary: '',
}

describe('NozzlePlanLine (#2166)', () => {
  it('says which nozzle each filament prints from, in one line', () => {
    render(<NozzlePlanLine plan={plan} />)
    expect(screen.getByTestId('nozzle-plan')).toHaveTextContent(
      'Nozzles:Mistletoe Green → right·Inland Black → left·0.4 High Flow',
    )
  })

  it("names each side's flow when they differ", () => {
    render(<NozzlePlanLine plan={{ ...plan, right_flow: 'standard' }} />)
    expect(screen.getByTestId('nozzle-plan')).toHaveTextContent('0.4 · left High Flow, right Standard')
  })

  it('shows nothing without a plan', () => {
    const { container } = render(<NozzlePlanLine plan={null} />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('NozzlePlanStep (#2166)', () => {
  it('names the planned side as Automatic and takes a side by hand', () => {
    const onSide = vi.fn()
    render(<NozzlePlanStep plan={plan} sides={{}} onSide={onSide} />)
    const first = screen.getByLabelText(/Slot 1: Mistletoe Green/)
    expect(first).toHaveDisplayValue('Automatic (right)')
    fireEvent.change(first, { target: { value: 'L' } })
    expect(onSide).toHaveBeenCalledWith(1, 'L')
    fireEvent.change(first, { target: { value: '' } })
    expect(onSide).toHaveBeenLastCalledWith(1, null)
  })
})
