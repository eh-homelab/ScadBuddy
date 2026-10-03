import type { PrintProgress, PrintRunResult } from '../../api/types'
import { WarningList } from '../FilamentPicker'
import { PrintProgressPanel } from '../PrintProgressPanel'

type Props = {
  result: PrintRunResult
  printerName: string | null
  progress: PrintProgress | null
  polling: boolean
}

/** What a started print shows in place of the steps: what was queued, and its progress (#89). */
export function QueuedPanel({ result, printerName, progress, polling }: Props) {
  const items = result.queue_item_ids ?? []
  /** #836 — the rack position each sliced group went out with; positions only, never a serial. */
  const picks = result.rack_picks ?? []
  const manyPlates = new Set(picks.map((pick) => pick.plate_id)).size > 1
  return (
    <div className="space-y-2 text-[13px] text-ink">
      <div data-testid="queued-items">
        <p>
          Sliced and queued for {printerName ?? 'the printer you chose'} —{' '}
          <span className="sb-num">{result.copies}</span>{' '}
          {result.copies === 1 ? 'copy' : 'copies'} in{' '}
          <span className="sb-num">{items.length}</span> {items.length === 1 ? 'item' : 'items'}.
        </p>
        {items.length > 0 && (
          <ul className="mt-1 space-y-0.5 text-[12px] text-muted">
            {items.map((itemId) => (
              <li key={itemId}>
                Queue <span className="sb-num">#{itemId}</span>
              </li>
            ))}
          </ul>
        )}
        {result.slice_job_id !== null && result.slice_job_id !== undefined && (
          <p className="mt-1 text-[12px] text-faint">
            Slice job <span className="sb-num">#{result.slice_job_id}</span>.
          </p>
        )}
      </div>
      {picks.length > 0 && (
        <ul data-testid="rack-picks" className="space-y-0.5 text-[12px] text-muted">
          {picks.map((pick) => (
            <li key={`${pick.plate_id}:${pick.group_id}`} data-testid="rack-pick">
              {manyPlates
                ? `Plate ${pick.plate_id}: rack nozzle position ${pick.position}`
                : `Rack nozzle: position ${pick.position}`}
            </li>
          ))}
        </ul>
      )}
      <WarningList warnings={result.warnings ?? []} testId="run-warnings" />
      <PrintProgressPanel progress={progress} polling={polling} />
    </div>
  )
}
