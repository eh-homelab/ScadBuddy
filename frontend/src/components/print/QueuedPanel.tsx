import type { OutputPlate, PrintProgress, PrintRunResult } from '../../api/types'
import { plateLabel } from '../../lib/plate'
import { Badge } from '@/components/ui/badge'
import { WarningList } from '../FilamentPicker'
import { PrintProgressPanel } from '../PrintProgressPanel'
import { LanRunControls } from './LanRunControls'

type Props = {
  result: PrintRunResult
  printerName: string | null
  progress: PrintProgress | null
  polling: boolean
  /** #986 — the source's plates, so a plate is named by what it holds. */
  plates?: readonly OutputPlate[]
}

/** What a started print shows in place of the steps: what was queued, and its progress (#89). */
export function QueuedPanel({ result, printerName, progress, polling, plates = [] }: Props) {
  // #3818 — the path the run reports it took: over the LAN there is no queue to name (§6.3).
  const native = result.path === 'native'
  const items = result.queue_item_ids ?? []
  /** #836 — the rack position each sliced group went out with; positions only, never a serial. */
  const picks = result.rack_picks ?? []
  const manyPlates = new Set(picks.map((pick) => pick.plate_id)).size > 1
  /** #4541 — the plate copies a LAN run starts later, one at a time. */
  const stopped = native ? (result.lan_stopped ?? null) : null
  // The result is the run as it succeeded; a LAN progress read says whether any still wait.
  const settledWaits = progress?.route === 'lan' && !progress.lan_run
  const waiting = native && stopped === null && !settledWaits ? (result.lan_waiting ?? []).length : 0
  /** Every print the run makes: each plate's copies, where `copies` is per plate. */
  const prints = (result.lan ?? []).length + waiting
  return (
    <div className="space-y-2 text-[13px] text-foreground">
      <div data-testid="queued-items">
        {native ? (
          <>
          <p>
            Sliced and sent to {printerName ?? 'the printer you chose'} over the LAN —{' '}
            <span className="sb-num">{result.copies}</span> {result.copies === 1 ? 'copy' : 'copies'}.
            {waiting > 0 && (
              <span data-testid="lan-waiting" className="block text-[12px] text-muted-foreground">
                The printer has no queue: ScadBuddy starts the other <span className="sb-num">{waiting}</span> of{' '}
                <span className="sb-num">{prints}</span> prints one at a time, each once the printer has finished
                the one before.
              </span>
            )}
            {stopped !== null && (
              <span data-testid="lan-stopped" className="block text-[12px] text-muted-foreground">
                {stopped.message}
              </span>
            )}
          </p>
          {/* #4942, #4939 — read from the progress, which follows the run as it is now. */}
          {progress?.lan_run && <LanRunControls control={progress.lan_run} printerName={printerName} className="mt-1.5" />}
          </>
        ) : (
        <p>
          Sliced and queued for {printerName ?? 'the printer you chose'} —{' '}
          <span className="sb-num">{result.copies}</span>{' '}
          {result.copies === 1 ? 'copy' : 'copies'} in{' '}
          <span className="sb-num">{items.length}</span> {items.length === 1 ? 'item' : 'items'}.
        </p>
        )}
        {items.length > 0 && (
          <ul className="mt-1.5 flex flex-wrap gap-1.5">
            {items.map((itemId) => (
              <li key={itemId}>
                <Badge variant="outline" className="font-normal text-muted-foreground">
                  Queue <span className="sb-num text-foreground">#{itemId}</span>
                </Badge>
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
        <ul data-testid="rack-picks" className="space-y-0.5 text-[12px] text-muted-foreground">
          {picks.map((pick) => (
            <li key={`${pick.plate_id}:${pick.group_id}`} data-testid="rack-pick">
              {manyPlates
                ? `${plateLabel(plates, pick.plate_id)}: rack nozzle position ${pick.position}`
                : `Rack nozzle: position ${pick.position}`}
            </li>
          ))}
        </ul>
      )}
      <WarningList warnings={result.warnings ?? []} testId="run-warnings" />
      <PrintProgressPanel progress={progress} polling={polling} plates={plates} />
    </div>
  )
}
