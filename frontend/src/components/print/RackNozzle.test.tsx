import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import type { RackPickView } from '../../api/types'
import { RackNozzleLine, RackNozzleStep } from './RackNozzle'

const rack: RackPickView = {
  group_id: null,
  position: 3,
  reason: 'already loaded with this color',
  unsafe_material: false,
  glow_unchecked: false,
  options: [
    { position: 2, nozzle_diameter: '0.4', flow: 'standard', color: '#00629B', nozzle_type: 'HS01', material: null, prints: 4, print_seconds: 7200 },
    { position: 3, nozzle_diameter: '0.4', flow: 'standard', color: '#FF6A13', nozzle_type: 'HS01', material: null, prints: 0, print_seconds: 0 },
  ],
}

describe('RackNozzleLine (#836)', () => {
  it('names the position, its size and flow, and the reason', () => {
    render(<RackNozzleLine rack={rack} algorithm="least_used" />)
    expect(screen.getByTestId('rack-nozzle-line')).toHaveTextContent(
      'Rack nozzle: position 3 (0.4 Standard) — already loaded with this color',
    )
  })

  it('says Bambuddy picks when the algorithm leaves it to Bambuddy', () => {
    render(<RackNozzleLine rack={{ ...rack, position: null, reason: null }} algorithm="bambuddy" />)
    expect(screen.getByTestId('rack-nozzle-line')).toHaveTextContent('Rack nozzle: Bambuddy picks at dispatch')
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

describe('RackNozzleStep (#836)', () => {
  it('changes the algorithm and picks a position by hand, or goes back to Automatic', () => {
    const onAlgorithm = vi.fn()
    const onPosition = vi.fn()
    render(
      <RackNozzleStep rack={rack} algorithm="least_used" position={null} onAlgorithm={onAlgorithm} onPosition={onPosition} />,
    )
    fireEvent.change(screen.getByLabelText('Rack algorithm'), { target: { value: 'oldest_first' } })
    expect(onAlgorithm).toHaveBeenCalledWith('oldest_first')
    fireEvent.change(screen.getByLabelText('Rack nozzle position'), { target: { value: '2' } })
    expect(onPosition).toHaveBeenLastCalledWith(2)
    fireEvent.change(screen.getByLabelText('Rack nozzle position'), { target: { value: '' } })
    expect(onPosition).toHaveBeenLastCalledWith(null)
    expect(screen.getByRole('option', { name: /Position 2 · 0.4 Standard · material unknown · 4 prints/ })).toBeInTheDocument()
  })
})
