import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import * as fixtures from '../mocks/fixtures'
import { PrintProgressPanel } from './PrintProgressPanel'

describe('PrintProgressPanel', () => {
  it('heads a pipeline run with how many copies have reached the queue', () => {
    render(<PrintProgressPanel progress={fixtures.pipelineProgress} polling />)

    const panel = screen.getByTestId('print-progress')
    expect(panel).toHaveTextContent('Run #12 — 2 of 2 copies queued')
  })

  it('names the printer per copy, or says Bambuddy has still to pick one', () => {
    render(<PrintProgressPanel progress={fixtures.pipelineProgress} polling />)

    expect(screen.getByTestId('print-progress-copy-0')).toHaveTextContent(
      'Copy 1 on 3DP-31B-598',
    )
    // `assigned_printer_name` is null until Bambuddy fans the run out; that is a normal
    // state, not a missing value.
    expect(screen.getByTestId('print-progress-copy-1')).toHaveTextContent(
      'Copy 2 on a printer Bambuddy picks',
    )
  })

  it('deep-links each queue entry from the URL the backend sent', () => {
    render(<PrintProgressPanel progress={fixtures.pipelineProgress} polling />)

    const link = within(screen.getByTestId('print-progress-copy-0')).getByRole('link', {
      name: '#4472',
    })
    expect(link).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/queue/4472',
    )
    // ScadBuddy runs inside Bambuddy's sandboxed iframe, so the link has to escape it.
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('shows the recorded failed run verbatim, with the fix the backend chose', () => {
    // `backend/tests/bambuddy/recordings/pipeline-run.json`: a slice that failed while
    // Bambuddy's own status still said `in_progress`.
    const progress = fixtures.failedRunProgress
    expect(progress.stage).toBe('failed')
    expect(progress.settled).toBe(true)

    render(<PrintProgressPanel progress={progress} polling={false} />)

    const error = screen.getByTestId('print-progress-error')
    expect(error).toHaveTextContent(
      'Slice failed: The selected printer is not compatible with the process preset in the 3mf.',
    )
    expect(error).toHaveClass('text-warn')
    expect(screen.getByTestId('print-progress-fix')).toHaveTextContent(
      'Bambuddy could not slice this plate. Choose a different pipeline or plate, or fix the model, and print again.',
    )
  })

  it('reads a waiting queue entry as information, not as a failure', () => {
    render(<PrintProgressPanel progress={fixtures.queuedSliceProgress} polling />)

    expect(screen.getByTestId('print-progress')).toHaveTextContent('Queue entry #4471')
    const reason = screen.getByText('No active H2C printers are idle')
    expect(reason).toHaveClass('text-faint')
    expect(screen.queryByTestId('print-progress-error')).not.toBeInTheDocument()
    expect(screen.queryByTestId('print-progress-fix')).not.toBeInTheDocument()
  })

  it('follows every plate of an all-plates print, not only the last', () => {
    const plate = fixtures.queuedSliceProgress.copies_detail![0]!
    render(
      <PrintProgressPanel
        progress={{
          ...fixtures.queuedSliceProgress,
          copies: 2,
          copies_detail: [
            { ...plate, plate_id: 1, queue_entry_id: 4470, stage: 'running' },
            { ...plate, plate_id: 2 },
          ],
        }}
        polling
      />,
    )

    expect(screen.getByTestId('print-progress')).toHaveTextContent('2 plates queued')
    expect(screen.getByTestId('print-progress-copy-0')).toHaveTextContent(
      'Plate 1 on 3DP-31B-598 as queue #4470 · running',
    )
    expect(screen.getByTestId('print-progress-copy-1')).toHaveTextContent(
      'Plate 2 on 3DP-31B-598 as queue #4471 · queued',
    )
  })

  it('says it is slicing before the queue entry exists', () => {
    render(
      <PrintProgressPanel
        progress={{ ...fixtures.queuedSliceProgress, queue_item_id: null, copies_detail: [] }}
        polling
      />,
    )

    expect(screen.getByTestId('print-progress')).toHaveTextContent('Slicing…')
  })

  it('says an all-plates print is slicing before any plate reaches the queue', () => {
    render(
      <PrintProgressPanel
        progress={{
          ...fixtures.queuedSliceProgress,
          queue_item_id: null,
          copies: 2,
          copies_detail: [
            { plate_id: 1, queue_entry_id: null, stage: 'running' },
            { plate_id: 2, queue_entry_id: null, stage: 'running' },
          ],
        }}
        polling
      />,
    )

    expect(screen.getByTestId('print-progress')).toHaveTextContent('Slicing…')
    expect(screen.getByTestId('print-progress')).not.toHaveTextContent('plates queued')
  })

  it('counts the plates that have reached the queue while the rest still slice', () => {
    render(
      <PrintProgressPanel
        progress={{
          ...fixtures.queuedSliceProgress,
          copies: 3,
          copies_detail: [
            { plate_id: 1, queue_entry_id: 4470, stage: 'queued' },
            { plate_id: 2, queue_entry_id: null, stage: 'running' },
            { plate_id: 3, queue_entry_id: null, stage: 'running' },
          ],
        }}
        polling
      />,
    )

    expect(screen.getByTestId('print-progress')).toHaveTextContent('1 of 3 plates queued')
  })

  it('says an all-plates print failed when a plate fails before it is queued', () => {
    render(
      <PrintProgressPanel
        progress={{
          ...fixtures.queuedSliceProgress,
          queue_item_id: null,
          stage: 'failed',
          settled: false,
          copies: 2,
          copies_detail: [
            { plate_id: 1, queue_entry_id: null, stage: 'failed' },
            { plate_id: 2, queue_entry_id: 4471, stage: 'running' },
          ],
        }}
        polling
      />,
    )

    const panel = screen.getByTestId('print-progress')
    expect(panel).toHaveTextContent('2 plates — failed')
    expect(panel).not.toHaveTextContent('Slicing…')
    expect(panel).not.toHaveTextContent('plates queued')
  })

  it('says a single-plate slice failed rather than that it is still slicing', () => {
    render(
      <PrintProgressPanel
        progress={{
          ...fixtures.queuedSliceProgress,
          queue_item_id: null,
          stage: 'failed',
          settled: true,
          copies_detail: [],
        }}
        polling={false}
      />,
    )

    const panel = screen.getByTestId('print-progress')
    expect(panel).toHaveTextContent('Slice — failed')
    expect(panel).not.toHaveTextContent('Slicing…')
  })

  it('spins only while the caller is still polling', () => {
    const { container, rerender } = render(
      <PrintProgressPanel progress={fixtures.pipelineProgress} polling />,
    )
    expect(container.querySelector('.animate-spin')).toBeInTheDocument()

    rerender(<PrintProgressPanel progress={fixtures.failedRunProgress} polling={false} />)
    expect(container.querySelector('.animate-spin')).not.toBeInTheDocument()
  })

  it('renders nothing for an output that has never been printed', () => {
    const { container } = render(<PrintProgressPanel progress={null} polling={false} />)

    expect(container).toBeEmptyDOMElement()
  })
})
