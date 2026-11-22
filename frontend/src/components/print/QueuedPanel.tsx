import { Link } from 'react-router'
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
  /** #6871 — the printer was busy, so nothing was sent: the run waits in ScadBuddy's queue. */
  const queued = native && stopped === null ? (result.lan_queued ?? null) : null
  /** Whether it waits for a print still running: the live read once the progress has one. */
  const forPrint = progress?.lan_run ? progress.lan_run.queued_for_print : Boolean(queued?.job)
  /** The printer reads idle (a restart, say): the plate may still hold the print before. */
  const idle = progress?.lan_run?.printer_idle ?? false
  const printer = printerName ?? 'the printer you chose'
  return (
    <div className="space-y-2 text-[13px] text-foreground">
      <div data-testid="queued-items">
        {queued !== null ? (
          <>
          <p>
            Queued for {printer}:{' '}
            {forPrint
              ? 'it is busy with another print, so nothing was sent to it yet.'
              : idle
                ? 'it reads idle, but its plate may still hold the print before, so nothing was sent to it yet. Clear it if so.'
                : 'its plate must be cleared first, so nothing was sent to it yet.'}{' '}
            ScadBuddy sends it once the printer has finished and someone says the plate is clear.
            <span className="mt-1 block text-[12px] text-muted-foreground">
              {queued.place !== null && queued.place !== undefined && (
                <span data-testid="lan-queued-place">
                  {queued.place <= 1 ? (
                    <>No earlier ScadBuddy run waits for {printer}.</>
                  ) : (
                    <>
                      <span className="sb-num">{queued.place - 1}</span> earlier{' '}
                      {queued.place === 2 ? 'run waits' : 'runs wait'} for {printer}. The run whose plate is said
                      clear first starts first.
                    </>
                  )}{' '}
                </span>
              )}
              <Link to="/prints" className="underline underline-offset-2 hover:text-foreground">
                Open the queue
              </Link>
            </span>
          </p>
          {progress?.lan_run && <LanRunControls control={progress.lan_run} printerName={printerName} className="mt-1.5" />}
          </>
        ) : native ? (
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
