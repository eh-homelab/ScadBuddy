import { useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError, printRunPoll } from '../api/client'
import type { BatchItem, PrintBatch, PrintRun } from '../api/types'
import { formatDuration } from '../lib/format'
import { PrintPicker } from './PrintPicker'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  /** The batch to show, kept in Postgres so a reload resumes it (#2672). */
  batchId: string
  /** Closed: the batch is forgotten, and whatever it queued stays queued. */
  onClose: () => void
}

/** Items still in the batch: a removed one leaves the strip and the review. */
function kept(batch: PrintBatch): BatchItem[] {
  return batch.items.filter((item) => !item.removed)
}

/** The last file chosen before `position`, whose choices carry forward as defaults. */
function carriedFor(batch: PrintBatch, position: number): BatchItem | null {
  const before = batch.items.filter((item) => item.position < position && !item.skipped && !item.removed && item.request)
  return before[before.length - 1] ?? null
}

/**
 * What the strip says about a file: chosen, skipped, not yet chosen, flagged (its print
 * would be refused) or warned (it queues, with something to set right first, which is
 * all a sliced file's mismatches ever are, #2671).
 */
function stateOf(item: BatchItem): 'chosen' | 'skipped' | 'flagged' | 'warned' | 'open' {
  if (item.skipped) return 'skipped'
  if ((item.problems ?? []).length > 0) return 'flagged'
  if (!item.request) return 'open'
  return (item.summary?.warnings ?? []).length > 0 ? 'warned' : 'chosen'
}

const STATE_LABEL = {
  chosen: 'chosen',
  skipped: 'skipped',
  flagged: 'would be refused',
  warned: 'chosen, with a warning',
  open: 'not chosen yet',
} as const

const STATE_MARK = { chosen: '✓ ', skipped: '', flagged: '⚠ ', warned: '! ', open: '' } as const

/**
 * #2672 — a batch print: one stepped print dialog over several library files. Each step
 * is the print dialog for one file, with Back, Skip and Next; the strip above it jumps to
 * any file and flags one whose check found something the run would refuse. The last step
 * is the review, where Queue all starts each chosen file's own print and reports each.
 * The batch and the step in view are saved as they change, so a reload resumes here.
 */
export function BatchPrintDialog({ batchId, onClose }: Props) {
  const [batch, setBatch] = useState<PrintBatch | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [queueing, setQueueing] = useState(false)
  /** Each started run as it stands, by id: the per-file result after Queue all. */
  const [runs, setRuns] = useState<Record<string, PrintRun>>({})

  /** The Queue all in flight, stopped when the dialog goes (#2718 review). */
  const queueRun = useRef<AbortController | null>(null)
  useEffect(() => () => queueRun.current?.abort(), [])

  useEffect(() => {
    let live = true
    api
      .getPrintBatch(batchId)
      .then((next) => live && setBatch(next))
      .catch((cause: unknown) => {
        if (live) setError(cause instanceof ApiError ? cause.detail : 'Could not read the batch print.')
      })
    return () => {
      live = false
    }
  }, [batchId])

  // Each started run, followed until it is queued or refused.
  const started = (batch?.items ?? []).flatMap((item) =>
    item.outcome?.status === 'started' && item.outcome.run_id ? [item.outcome.run_id] : [],
  )
  const following = started.filter((id) => !runs[id] || runs[id].status === 'running').join(',')
  /** At once for a run never read, then at the print follow's pace. */
  const readOnce = following !== '' && following.split(',').every((id) => runs[id])
  useEffect(() => {
    if (following === '') return
    let live = true
    const timer = window.setTimeout(() => {
      void Promise.all(following.split(',').map((id) => api.getPrintRun(id).catch(() => null))).then((read) => {
        if (!live) return
        setRuns((now) => {
          const next = { ...now }
          for (const run of read) if (run) next[run.id] = run
          return next
        })
      })
    }, readOnce ? printRunPoll.intervalMs : 0)
    return () => {
      live = false
      window.clearTimeout(timer)
    }
  }, [following, readOnce, runs])

  // A run that failed reopens its file, to edit and queue again: read the batch again
  // for it (#2718 review). One that may have queued stays started, so this reads once.
  const failedRuns = started.filter((id) => runs[id]?.status === 'failed').join(',')
  useEffect(() => {
    if (failedRuns === '') return
    let live = true
    api
      .getPrintBatch(batchId)
      .then((next) => live && setBatch(next))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [batchId, failedRuns])

  async function save(change: () => Promise<PrintBatch>) {
    setBusy(true)
    setError(null)
    try {
      setBatch(await change())
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not save the batch print.')
    } finally {
      setBusy(false)
    }
  }

  function close() {
    // A file still being checked may yet print: the batch is kept, so Queue all can
    // follow that run rather than a new batch queueing the file twice (#2718 review).
    if (!batch?.items.some((item) => item.outcome?.status === 'pending')) {
      void api.deletePrintBatch(batchId).catch(() => undefined)
    }
    onClose()
  }

  if (!batch) {
    return (
      <Dialog open title="Print files" onClose={onClose}>
        {error ? (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        ) : (
          <p className="flex items-center gap-2 text-[13px] text-muted">
            <Spinner /> Reading the batch print
          </p>
        )}
      </Dialog>
    )
  }

  const items = kept(batch)
  const total = batch.items.length
  /** The review is the step after the last file. */
  const reviewing = batch.step >= total || batch.status === 'queued'
  const current = reviewing ? undefined : batch.items.find((item) => item.position === batch.step)
  const goTo = (step: number) => void save(() => api.putPrintBatchStep(batchId, step))
  /** The next kept file after `position`, or the review. */
  const after = (position: number) => items.find((item) => item.position > position)?.position ?? total
  const before = (position: number) => [...items].reverse().find((item) => item.position < position)?.position

  const strip = (go: (step: number) => void) => (
    <nav aria-label="Batch files" className="mb-3 flex flex-wrap gap-1.5" data-testid="batch-strip">
      {items.map((item, at) => {
        const state = stateOf(item)
        const here = item.position === batch.step && !reviewing
        return (
          <button
            key={item.position}
            type="button"
            aria-current={here ? 'step' : undefined}
            aria-label={`File ${at + 1}: ${item.filename}, ${STATE_LABEL[state]}`}
            data-testid={`batch-step-${item.position}`}
            data-state={state}
            disabled={busy || batch.status === 'queued'}
            onClick={() => go(item.position)}
            className={`max-w-48 truncate rounded border px-2 py-0.5 text-[12px] ${
              here ? 'border-accent text-ink' : 'border-line text-muted'
            } ${state === 'flagged' ? 'text-warn' : ''} ${state === 'skipped' ? 'line-through' : ''}`}
          >
            {STATE_MARK[state]}
            {at + 1}. {item.filename}
          </button>
        )
      })}
      <button
        type="button"
        aria-current={reviewing ? 'step' : undefined}
        data-testid="batch-step-review"
        disabled={busy || batch.status === 'queued'}
        onClick={() => go(total)}
        className={`rounded border px-2 py-0.5 text-[12px] ${reviewing ? 'border-accent text-ink' : 'border-line text-muted'}`}
      >
        Review
      </button>
    </nav>
  )

  if (current) {
    const at = items.findIndex((item) => item.position === current.position)
    const own = current.request ?? null
    const carried = own === null ? carriedFor(batch, current.position) : null
    const back = before(current.position)
    return (
      <PrintPicker
        // Each file's step starts afresh, on its own seed.
        key={current.position}
        open
        source={{ kind: 'library', file: { id: current.library_file_id, filename: current.filename } }}
        onClose={close}
        onRan={() => undefined}
        batch={{
          title: `File ${at + 1} of ${items.length} — ${current.filename}`,
          strip: (jump) => (
            <>
              {strip(jump)}
              {error && (
                <p role="alert" className="mb-2 text-[13px] text-warn">
                  {error}
                </p>
              )}
            </>
          ),
          seed: own ?? carried?.request ?? null,
          seedIsOwn: own !== null,
          seedSlots: carried?.summary?.slots ?? [],
          onBack: back === undefined ? null : () => goTo(back),
          // One write each, the choice and the step together (#2718 review): a choice
          // saved with its step left behind would look lost.
          onSkip: () =>
            void save(() =>
              api.putPrintBatchItem(batchId, current.position, {
                skipped: true,
                removed: false,
                step: after(current.position),
              }),
            ),
          onNext: (choice) =>
            void save(() =>
              api.putPrintBatchItem(batchId, current.position, {
                ...choice,
                skipped: false,
                removed: false,
                step: after(current.position),
              }),
            ),
          // Left through the strip: the choices go with the step, as with Next (#2718 review).
          onJump: (step, choice) =>
            choice
              ? void save(() =>
                  api.putPrintBatchItem(batchId, current.position, { ...choice, skipped: current.skipped, removed: false, step }),
                )
              : goTo(step),
          last: after(current.position) === total,
          busy,
        }}
      />
    )
  }

  const queued = batch.status === 'queued'
  const toQueue = items.filter(
    (item) => !item.skipped && item.request && item.outcome?.status !== 'started',
  )
  const sentBefore = items.some((item) => item.outcome)

  async function queueAll() {
    const run = new AbortController()
    queueRun.current = run
    setQueueing(true)
    setError(null)
    try {
      setBatch(await api.queuePrintBatch(batchId, setBatch, run.signal))
    } catch (cause) {
      if (run.signal.aborted) return
      setError(cause instanceof ApiError ? cause.detail : 'Could not queue the batch print.')
    } finally {
      if (!run.signal.aborted) setQueueing(false)
    }
  }

  return (
    <Dialog
      open
      size="wide"
      title={queued ? 'Batch print queued' : `Review ${items.length} files`}
      description={
        queued
          ? 'Each file went to its own print. One that was refused did not stop the rest.'
          : sentBefore
            ? 'Some files are not queued yet. Edit a refused one, then Queue all again: a file still being checked is followed, never queued twice.'
            : 'Check each file, then Queue all. A file that is refused does not stop the rest.'
      }
      // Escape and ✕ are Cancel, not Back (#2718 review).
      onClose={() => {
        if (!queueing) close()
      }}
      footer={
        queued ? (
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={close} disabled={queueing}>
              Cancel
            </Button>
            {before(total) !== undefined && (
              <Button onClick={() => goTo(before(total) as number)} disabled={queueing || busy}>
                Back
              </Button>
            )}
            <Button
              variant="primary"
              onClick={() => void queueAll()}
              disabled={queueing || busy || toQueue.length === 0}
              data-testid="batch-queue-all"
              {...USER_ONLY}
            >
              {queueing && <Spinner />}
              Queue all ({toQueue.length})
            </Button>
          </>
        )
      }
    >
      {strip(goTo)}
      {error && (
        <p role="alert" className="mb-2 text-[13px] text-warn">
          {error}
        </p>
      )}
      <ul className="divide-y divide-line" aria-label="Files to print">
        {items.map((item) => (
          <ReviewRow
            key={item.position}
            item={item}
            run={item.outcome?.run_id ? runs[item.outcome.run_id] : undefined}
            queued={queued}
            busy={busy || queueing}
            onEdit={() => goTo(item.position)}
            onRemove={() =>
              void save(() =>
                api.putPrintBatchItem(batchId, item.position, {
                  request: item.request ?? null,
                  skipped: item.skipped ?? false,
                  removed: true,
                  summary: item.summary ?? null,
                  problems: item.problems ?? [],
                }),
              )
            }
          />
        ))}
      </ul>
    </Dialog>
  )
}

function ReviewRow({
  item,
  run,
  queued,
  busy,
  onEdit,
  onRemove,
}: {
  item: BatchItem
  run: PrintRun | undefined
  queued: boolean
  busy: boolean
  onEdit: () => void
  onRemove: () => void
}) {
  const summary = item.summary
  const problems = item.problems ?? []
  const details = [
    summary?.printer,
    summary?.plate,
    summary?.print_seconds ? formatDuration(summary.print_seconds) : null,
    summary?.filament_g ? `${Math.round(summary.filament_g * 10) / 10} g` : null,
  ].filter(Boolean)
  return (
    <li className="flex flex-wrap items-start gap-x-3 gap-y-1 py-2" data-testid={`batch-row-${item.position}`}>
      <div className="min-w-0 flex-1">
        <p className="truncate text-[13px] text-ink">{item.filename}</p>
        {item.skipped ? (
          <p className="text-[12px] text-muted">Skipped: it is not queued.</p>
        ) : !item.request ? (
          <p className="text-[12px] text-warn">No choices yet: Edit it, or it is not queued.</p>
        ) : (
          <>
            {details.length > 0 && <p className="text-[12px] text-muted">{details.join(' · ')}</p>}
            {(summary?.spools ?? []).length > 0 && (
              <p className="text-[12px] text-muted">{(summary?.spools ?? []).join(', ')}</p>
            )}
          </>
        )}
        {problems.map((problem) => (
          <p key={problem} className="text-[12px] text-warn">
            ⚠ {problem}
          </p>
        ))}
        {!item.skipped &&
          (summary?.warnings ?? []).map((warning) => (
            <p key={warning} className="text-[12px] text-muted">
              ! {warning}
            </p>
          ))}
        <Outcome item={item} run={run} />
      </div>
      {!queued && !locked(item) && (
        <div className="flex gap-1.5">
          <Button size="sm" onClick={onEdit} disabled={busy} aria-label={`Edit ${item.filename}`}>
            Edit
          </Button>
          <Button size="sm" variant="ghost" onClick={onRemove} disabled={busy} aria-label={`Remove ${item.filename}`}>
            Remove
          </Button>
        </div>
      )}
    </li>
  )
}

/** A file whose print has started, or may have: its choices no longer change. */
function locked(item: BatchItem): boolean {
  return item.outcome?.status === 'started' || item.outcome?.status === 'pending'
}

/** What Queue all did with one file, and how its run ended once it has. */
function Outcome({ item, run }: { item: BatchItem; run: PrintRun | undefined }) {
  const outcome = item.outcome
  if (!outcome) return null
  if (outcome.status === 'pending') {
    return (
      <p role="status" className="text-[12px] text-muted" data-testid={`batch-outcome-${item.position}`}>
        Still being checked{outcome.detail ? `: ${outcome.detail}` : '.'} Queue all again to follow it.
      </p>
    )
  }
  if (outcome.status !== 'started') {
    return (
      <p role="status" className="text-[12px] text-warn" data-testid={`batch-outcome-${item.position}`}>
        Not queued: {outcome.detail ?? 'the print failed before it was queued.'}
      </p>
    )
  }
  const text =
    run?.status === 'succeeded'
      ? 'Queued in Bambuddy.'
      : run?.status === 'failed'
        ? `Not queued: ${run.error?.detail ?? 'the print failed before it was queued.'}`
        : 'Queueing…'
  return (
    <p
      role="status"
      className={`text-[12px] ${run?.status === 'failed' ? 'text-warn' : 'text-ink'}`}
      data-testid={`batch-outcome-${item.position}`}
    >
      {text}
    </p>
  )
}
