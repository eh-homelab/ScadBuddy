import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PrintRunResult } from '../../api/types'
import { queuedResult } from '../../mocks/choices'
import { QueuedPanel } from './QueuedPanel'

function renderPanel(result: PrintRunResult) {
  return render(<QueuedPanel result={result} printerName="H2C" progress={null} polling={false} />)
}

describe('QueuedPanel · rack picks (#836)', () => {
  it('names the rack position each sliced group was sent with', () => {
    renderPanel({ ...queuedResult, rack_picks: [{ plate_id: 1, group_id: 0, position: 3 }] })
    expect(screen.getByTestId('rack-picks')).toHaveTextContent('Rack nozzle: position 3')
    expect(screen.getByTestId('rack-picks')).not.toHaveTextContent(/plate/i)
  })

  it('says which plate each pick was for when the run has more than one plate', () => {
    renderPanel({
      ...queuedResult,
      rack_picks: [
        { plate_id: 1, group_id: 0, position: 3 },
        { plate_id: 2, group_id: 0, position: 5 },
      ],
    })
    const items = screen.getAllByTestId('rack-pick')
    expect(items.map((item) => item.textContent)).toEqual([
      'Plate 1: rack nozzle position 3',
      'Plate 2: rack nozzle position 5',
    ])
  })

  it('shows nothing about the rack when no pick was sent', () => {
    renderPanel({ ...queuedResult, rack_picks: [] })
    expect(screen.queryByTestId('rack-picks')).toBeNull()
    renderPanel({ ...queuedResult })
    expect(screen.queryByTestId('rack-picks')).toBeNull()
  })
})
