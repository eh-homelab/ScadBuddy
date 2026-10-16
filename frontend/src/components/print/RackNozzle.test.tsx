import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RackPickView } from '../../api/types'
import { RackNozzleLine, RackNozzleStep } from './RackNozzle'

const two = { position: 2, nozzle_diameter: '0.4', flow: 'standard', color: '#00629B', nozzle_type: 'HS01', material: null, prints: 4, print_seconds: 7200, pending: 0 } as const
const three = { position: 3, nozzle_diameter: '0.4', flow: 'standard', color: '#FF6A13', nozzle_type: 'HS01', material: null, prints: 0, print_seconds: 0, pending: 0 } as const
const rack: RackPickView = {
  group_id: null,
  position: 3,
  reason: 'already loaded with this color',
  unsafe_material: false,
  glow_unchecked: false,
  options: [two, three],
}

describe('RackNozzleLine (#836)', () => {
  it('names the position, its size and flow, and the reason', () => {
    render(<RackNozzleLine rack={rack} algorithm="least_used" />)
    expect(screen.getByTestId('rack-nozzle-line')).toHaveTextContent(
      'Right nozzle from the rack: position 3 (0.4 Standard) — already loaded with this color',
    )
  })

  it('names the pick without a dash when there is no reason', () => {
    render(<RackNozzleLine rack={{ ...rack, reason: null }} algorithm="least_used" />)
    expect(screen.getByTestId('rack-nozzle-line').textContent).toBe('Right nozzle from the rack: position 3 (0.4 Standard)')
  })

  it('says Bambuddy picks when the algorithm leaves it to Bambuddy', () => {
    render(<RackNozzleLine rack={{ ...rack, position: null, reason: null }} algorithm="bambuddy" />)
    expect(screen.getByTestId('rack-nozzle-line')).toHaveTextContent('Right nozzle from the rack: Bambuddy picks at dispatch')
  })

  it('says when Glow could not be checked', () => {
    render(<RackNozzleLine rack={{ ...rack, glow_unchecked: true }} algorithm="least_used" />)
    expect(screen.getByText(/Glow could not be checked/)).toBeInTheDocument()
  })

  it('shows nothing without a rack', () => {
    const { container } = render(<RackNozzleLine rack={null} algorithm="least_used" />)
    expect(container).toBeEmptyDOMElement()
  })
})

describe('RackNozzleStep (#836, #2166)', () => {
  it('is one list, Automatic or a position by hand, with how Automatic ranks them', () => {
    const onAlgorithm = vi.fn()
    const onPosition = vi.fn()
    render(
      <RackNozzleStep rack={rack} algorithm="least_used" position={null} onAlgorithm={onAlgorithm} onPosition={onPosition} />,
    )
    expect(screen.getByRole('radio', { name: 'Automatic' })).toBeChecked()
    fireEvent.change(screen.getByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    expect(onAlgorithm).toHaveBeenCalledWith('oldest_first')
    fireEvent.click(screen.getByRole('radio', { name: /Position 2/ }))
    expect(onPosition).toHaveBeenLastCalledWith(2)
  })

  it('names each hotend by the filament it last ran, never by a hex', () => {
    const ran: RackPickView = {
      ...rack,
      options: [{ ...two, filament_type: 'PLA', color_word: 'blue' }, three],
    }
    render(<RackNozzleStep rack={ran} algorithm="least_used" position={2} onAlgorithm={vi.fn()} onPosition={vi.fn()} />)
    const step = screen.getByTestId('rack-step')
    expect(step).toHaveTextContent('Position 2 · 0.4 Standard · last ran blue PLA')
    expect(step.textContent).not.toMatch(/#00629B/i)
    expect(screen.getByRole('radio', { name: /Position 2/ })).toBeChecked()
  })

  it('says how many prints are queued on a position but not settled (#1079)', () => {
    const queued: RackPickView = { ...rack, options: [{ ...two, pending: 2 }, three] }
    render(<RackNozzleStep rack={queued} algorithm="least_used" position={null} onAlgorithm={vi.fn()} onPosition={vi.fn()} />)
    expect(screen.getByTestId('rack-step')).toHaveTextContent('4 prints · 2 queued')
  })
})
