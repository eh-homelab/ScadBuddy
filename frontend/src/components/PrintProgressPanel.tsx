import type { CopyProgress, PrintProgress } from '../api/types'
import { Spinner } from './ui/Spinner'

/**
 * Where a print got to (#89), for whichever of Bambuddy's two routes ran.
 *
 * The panel is deliberately a reader: every judgement it could make has already been
 * made by the backend, and remaking any of them here would be wrong in a way that is
 * invisible until a real print fails.
 *
 * - **`settled` decides when the caller stops polling, not this panel.** A failed run
 *   still reports a copy in progress, so nothing here treats a stage or a counter as
 *   "finished".
 * - **`waiting_reason` is not an error.** It is Bambuddy's own sentence for why a queued
 *   entry is not printing yet ("No active H2C printers are idle"), and every normal
 *   queue wait has one. It reads muted, beside the copy it belongs to; `error_message`
 *   is the failure and is the only thing in `text-warn`.
 * - **`fix` is the action that applies**, picked by the backend from *where* the print
 *   failed rather than from what the message said. The panel shows it as it came and
 *   never string-matches `error_message` to offer advice of its own.
 */

interface Props {
  /** `null` when this output has never been printed — there is nothing to show. */
  progress: PrintProgress | null
  polling: boolean
}

/**
 * Bambuddy sends one queue page for the whole print, so a per-entry link is built from
 * it. `target=_blank` because ScadBuddy renders inside Bambuddy's sandboxed iframe and a
 * same-frame navigation would replace the app (spec §1).
 */
function queueEntryUrl(progress: PrintProgress, entryId: number): string {
  return `${progress.bambuddy_url}/${entryId}`
}

function headline(progress: PrintProgress): string {
  if (progress.route === 'pipeline') {
    // Counted from the entries rather than from `copies_completed`: on this route the
    // run is accepted before the queue entries exist, so "queued" means a copy has an
    // entry id — which is exactly the thing the poll is waiting for.
    const queued = (progress.copies_detail ?? []).filter(
      (copy) => copy.queue_entry_id !== null && copy.queue_entry_id !== undefined,
    ).length
    const run = progress.pipeline_run_id ?? '?'
    return `Run #${run} — ${queued} of ${progress.copies} ${
      progress.copies === 1 ? 'copy' : 'copies'
    } queued`
  }
  // The slice-and-queue route has no run: the queue item is the whole print, and until
  // the plate has sliced it does not exist yet.
  if (progress.queue_item_id === null || progress.queue_item_id === undefined) {
    return 'Slicing…'
  }
  return `Queue entry #${progress.queue_item_id}`
}

function copyLabel(copy: CopyProgress): string {
  // The queue route repeats through `quantity` and carries no `copy_index`, so there is
  // nothing to number: the entry itself is the identity.
  if (copy.copy_index === null || copy.copy_index === undefined) return 'Copy'
  return `Copy ${copy.copy_index + 1}`
}

export function PrintProgressPanel({ progress, polling }: Props) {
  if (!progress) return null

  const copies = progress.copies_detail ?? []

  return (
    <div className="space-y-2 text-[13px] text-ink" data-testid="print-progress">
      <p className="flex items-center gap-2">
        {polling && <Spinner />}
        <span>{headline(progress)}</span>
      </p>

      {copies.length > 0 && (
        <ul className="space-y-1 text-[12px] text-muted">
          {copies.map((copy, index) => (
            <li key={index} data-testid={`print-progress-copy-${index}`}>
              <span>
                {copyLabel(copy)} on {copy.printer_name ?? 'a printer Bambuddy picks'}
              </span>
              {copy.queue_entry_id ? (
                <>
                  {' as queue '}
                  <a
                    className="sb-num underline decoration-dotted underline-offset-2 hover:text-ink"
                    href={queueEntryUrl(progress, copy.queue_entry_id)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    #{copy.queue_entry_id}
                  </a>
                </>
              ) : null}
              {' · '}
              {copy.stage}
              {copy.waiting_reason && (
                <span className="mt-0.5 block text-faint">{copy.waiting_reason}</span>
              )}
            </li>
          ))}
        </ul>
      )}

      {progress.error_message && (
        <p className="text-[12px] text-warn" data-testid="print-progress-error">
          {progress.error_message}
        </p>
      )}
      {progress.fix && (
        <p className="text-[12px] text-muted" data-testid="print-progress-fix">
          {progress.fix}
        </p>
      )}
    </div>
  )
}
