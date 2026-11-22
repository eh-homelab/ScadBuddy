import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { describe, expect, it } from 'vitest'
import type { PrintProgress, PrintRunResult } from '../../api/types'
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

  it('says why the rest were stopped in place of when they start', () => {
    const message = 'The other prints were cancelled before they started.'
    renderPanel({
      ...queuedResult,
      path: 'native',
      copies: 2,
      lan_waiting: [1],
      lan_stopped: { stage: 'cancelled', message },
    })
    expect(screen.queryByTestId('lan-waiting')).toBeNull()
    expect(screen.getByTestId('lan-stopped')).toHaveTextContent(message)
  })

  it('says nothing more once every one has started', () => {
    renderPanel({ ...queuedResult, path: 'native', copies: 2, lan_waiting: [] })
    expect(screen.queryByTestId('lan-waiting')).toBeNull()
  })

  // #4942, #4939 — the run's controls follow its progress, which reads the run as it is now.
  const lanProgress: PrintProgress = {
    route: 'lan',
    stage: 'running',
    settled: false,
    copies: 2,
    copies_completed: 1,
    copies_failed: 0,
    copies_cancelled: 0,
    copies_in_progress: 0,
    bambuddy_url: '',
  }

  it("offers the run's controls while its progress says prints are still to start", () => {
    const progress: PrintProgress = {
      ...lanProgress,
      lan_run: { run_id: 'r'.repeat(32), waiting: 1, awaiting_clear: true, queued: false, queued_for_print: false, printer_idle: false },
    }
    render(
      <QueuedPanel result={{ ...queuedResult, path: 'native', copies: 2, lan_waiting: [1] }} printerName="H2C" progress={progress} polling />,
    )
    expect(screen.getByTestId('lan-run-controls')).toHaveTextContent('Clear the plate of H2C')
    expect(screen.getByRole('button', { name: 'Stop the rest' })).toBeInTheDocument()
  })

  it('offers no controls once the progress names none', () => {
    const progress: PrintProgress = { ...lanProgress, lan_run: null }
    render(
      <QueuedPanel result={{ ...queuedResult, path: 'native', copies: 2, lan_waiting: [1] }} printerName="H2C" progress={progress} polling />,
    )
    expect(screen.queryByTestId('lan-run-controls')).toBeNull()
  })
})

// #6871 — the printer was busy, so the run sent nothing and waits in ScadBuddy's queue.
describe('QueuedPanel · a run queued on a busy printer', () => {
  const theirs = { subtask_name: 'Bambu Refill spool', started_at: '2026-11-21T14:29:33Z' }
  const queued: PrintRunResult = {
    ...queuedResult,
    path: 'native',
    lan: [],
    lan_waiting: [1],
    lan_queued: { state: 'RUNNING', job: theirs, place: 2 },
  }

  function renderQueued(result: PrintRunResult) {
    render(
      <MemoryRouter>
        <QueuedPanel result={result} printerName="H2C" progress={null} polling={false} />
      </MemoryRouter>,
    )
  }

  it('says it is queued, that nothing was sent, its place, and links to the queue', () => {
    renderQueued(queued)
    const items = screen.getByTestId('queued-items')
    expect(items).toHaveTextContent('Queued for H2C: it is busy with another print, so nothing was sent to it yet.')
    expect(items).toHaveTextContent(
      'ScadBuddy sends it once the printer has finished and someone says the plate is clear.',
    )
    expect(screen.getByTestId('lan-queued-place')).toHaveTextContent(
      '1 earlier run waits for H2C. The run whose plate is said clear first starts first.',
    )
    expect(screen.getByRole('link', { name: 'Open the queue' })).toHaveAttribute('href', '/prints')
    expect(items).not.toHaveTextContent('Sliced and sent')
    expect(items).not.toHaveTextContent('may still have been queued')
  })

  it('says the plate must be cleared when the printer holds an ended print', () => {
    renderQueued({ ...queued, lan_queued: { state: 'FINISH', job: null, place: 1 } })
    expect(screen.getByTestId('queued-items')).toHaveTextContent(
      'Queued for H2C: its plate must be cleared first, so nothing was sent to it yet.',
    )
    expect(screen.getByTestId('lan-queued-place')).toHaveTextContent('No earlier ScadBuddy run waits for H2C.')
  })

  // Review of #6939: the print it waited for may have ended since; the progress reads it live.
  it('turns to the plate once the live read says the print it waited for has ended', () => {
    const control = { run_id: 'r'.repeat(32), waiting: 1, awaiting_clear: false, queued: true }
    const progress = (queued_for_print: boolean): PrintProgress => ({
      route: 'lan',
      stage: 'queued',
      settled: false,
      copies: 1,
      copies_completed: 0,
      copies_failed: 0,
      copies_cancelled: 0,
      copies_in_progress: 0,
      bambuddy_url: '',
      lan_run: { ...control, queued_for_print, printer_idle: false },
    })
    const { rerender } = render(
      <MemoryRouter>
        <QueuedPanel result={queued} printerName="H2C" progress={progress(true)} polling />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('queued-items')).toHaveTextContent('it is busy with another print')
    expect(screen.getByTestId('lan-run-controls')).toHaveTextContent('has finished the print it is running')
    rerender(
      <MemoryRouter>
        <QueuedPanel result={queued} printerName="H2C" progress={progress(false)} polling />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('queued-items')).toHaveTextContent('its plate must be cleared first')
    expect(screen.getByTestId('lan-run-controls')).toHaveTextContent(
      'Queued until the plate of H2C is clear: it holds a print that has ended.',
    )
  })

  it('says the plate may still hold the print before when the printer reads idle', () => {
    const progress: PrintProgress = {
      route: 'lan',
      stage: 'queued',
      settled: false,
      copies: 1,
      copies_completed: 0,
      copies_failed: 0,
      copies_cancelled: 0,
      copies_in_progress: 0,
      bambuddy_url: '',
      lan_run: { run_id: 'r'.repeat(32), waiting: 1, awaiting_clear: false, queued: true, queued_for_print: false, printer_idle: true },
    }
    render(
      <MemoryRouter>
        <QueuedPanel result={queued} printerName="H2C" progress={progress} polling />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('queued-items')).toHaveTextContent(
      'Queued for H2C: it reads idle, but its plate may still hold the print before, so nothing was sent to it yet. Clear it if so.',
    )
    expect(screen.getByTestId('lan-run-controls')).toHaveTextContent('It reads idle')
  })

  it('names no place when it could not be read', () => {
    renderQueued({ ...queued, lan_queued: { state: 'RUNNING', job: theirs, place: null } })
    expect(screen.queryByTestId('lan-queued-place')).toBeNull()
    expect(screen.getByRole('link', { name: 'Open the queue' })).toBeInTheDocument()
  })
})
