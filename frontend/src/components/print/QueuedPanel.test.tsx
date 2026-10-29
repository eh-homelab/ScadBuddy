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

  it("names each pick's plate by what it holds when the plate has a name (#986)", () => {
    render(
      <QueuedPanel
        result={{
          ...queuedResult,
          rack_picks: [
            { plate_id: 1, group_id: 0, position: 3 },
            { plate_id: 2, group_id: 0, position: 5 },
          ],
        }}
        printerName="H2C"
        progress={null}
        polling={false}
        plates={[
          { index: 1, has_thumbnail: true, name: 'Lid' },
          { index: 2, has_thumbnail: true, name: '' },
        ]}
      />,
    )
    expect(screen.getAllByTestId('rack-pick').map((item) => item.textContent)).toEqual([
      'Lid: rack nozzle position 3',
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

// #3818 — "over the LAN" is the path the run reports, never the dialog's guess at it.
describe('QueuedPanel · the path the run took', () => {
  it('says the print went over the LAN only when the run reports the native path', () => {
    renderPanel({ ...queuedResult, path: 'native' })
    expect(screen.getByTestId('queued-items')).toHaveTextContent('Sliced and sent to H2C over the LAN')
  })

  it("names Bambuddy's queue when the run went through it", () => {
    renderPanel({ ...queuedResult, path: 'bambuddy' })
    expect(screen.getByTestId('queued-items')).toHaveTextContent('Sliced and queued for H2C')
    expect(screen.getByTestId('queued-items')).not.toHaveTextContent('over the LAN')
  })
})

// #4541 — the printer has no queue, so a LAN run of several plate copies is one.
describe('QueuedPanel · a LAN run of several plate copies', () => {
  it('says the rest start one at a time, as the printer finishes each', () => {
    const started = { subtask_name: 'scadbuddy-run-p1', started_at: '2026-10-28T12:00:00Z', plate_id: 1 }
    // Two plates of two copies: `copies` is per plate, so the prints are counted.
    renderPanel({ ...queuedResult, path: 'native', copies: 2, lan: [started], lan_waiting: [1, 2, 2] })
    expect(screen.getByTestId('lan-waiting')).toHaveTextContent(
      'The printer has no queue: ScadBuddy starts the other 3 of 4 prints one at a time, each once the printer has finished the one before.',
    )
  })

  it('says nothing more once every one has started', () => {
    renderPanel({ ...queuedResult, path: 'native', copies: 2, lan_waiting: [] })
    expect(screen.queryByTestId('lan-waiting')).toBeNull()
  })
})
