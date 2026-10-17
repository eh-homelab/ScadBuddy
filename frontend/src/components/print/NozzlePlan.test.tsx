import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { choose } from '../../test/select'
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
  it('names the planned side as Automatic and takes a side by hand', async () => {
    const user = userEvent.setup()
    const onSide = vi.fn()
    const { rerender } = render(<NozzlePlanStep plan={plan} sides={{}} onSide={onSide} />)
    const first = screen.getByRole('combobox', { name: /Slot 1: Mistletoe Green/ })
    expect(first).toHaveTextContent('Automatic (right)')
    await choose(user, first, 'Left')
    expect(onSide).toHaveBeenCalledWith(1, 'L')
    rerender(<NozzlePlanStep plan={plan} sides={{ '1': 'L' }} onSide={onSide} />)
    await choose(user, first, 'Automatic')
    expect(onSide).toHaveBeenLastCalledWith(1, null)
  })

  it('says nothing of regrouping for a plan held to one side (#2737 review)', () => {
    const oneSide = { ...plan, slots: (plan.slots ?? []).map((slot) => ({ ...slot, side: 'L' as const })) }
    render(<NozzlePlanStep plan={oneSide} sides={{}} onSide={vi.fn()} />)
    expect(screen.queryByTestId('nozzle-side-hint')).toBeNull()
  })

  it('says the slicer groups filaments itself while every side is Automatic (#2737 review)', () => {
    render(<NozzlePlanStep plan={plan} sides={{}} onSide={vi.fn()} />)
    expect(screen.getByTestId('nozzle-side-hint')).toHaveTextContent(
      'Left on Automatic, the slicer groups filaments itself to cut purging, so it may put one on the other side: choose a side to keep it there.',
    )
  })

  it('says a side set here is kept (#3955)', () => {
    render(<NozzlePlanStep plan={plan} sides={{ '1': 'L' }} onSide={vi.fn()} />)
    const hint = screen.getByTestId('nozzle-side-hint')
    expect(hint).toHaveTextContent('With a side chosen here, the slice keeps every filament on the side shown.')
    expect(hint).not.toHaveTextContent('groups filaments itself')
  })
})
