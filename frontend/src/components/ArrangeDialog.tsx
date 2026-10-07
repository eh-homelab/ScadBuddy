import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import type { ArrangeRequest, Output } from '../api/types'
import {
  backfillFailures,
  backfillIds,
  backfillNote,
  backfillOutputs,
  GOAL_LABELS,
  needsBackfill,
  runArrange,
  type ArrangeGoal,
  type Arranged,
} from '../lib/arrange'
import { BackfillProgress, BackfillPrompt } from './BackfillPrompt'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'

type Props = {
  open: boolean
  slug: string
  outputs: Output[]
  onClose: () => void
  onArranged: (arranged: Arranged) => void
}

/** Each object of each output that has them, as a row with its key. */
function rowsOf(outputs: Output[]) {
  return outputs.flatMap((output) =>
    (output.manifest ?? []).map((object) => ({ output, object, key: `${output.id}:${object.part}` })),
  )
}

/**
 * #314 — objects from several outputs onto shared plates (spec 2026-09-27 §7). Each
 * object of each selected output is a row with its copies; Arrange lays them out for
 * the goal on the configured printer's plate, with no re-render, and saves the result.
 * An output saved before Arrange has no objects: Arrange asks to re-render it first,
 * then arranges with it (#902).
 */
export function ArrangeDialog({ open, slug, outputs: given, onClose, onArranged }: Props) {
  /** Outputs re-rendered here, read back with their objects. */
  const [refreshed, setRefreshed] = useState<Record<string, Output>>({})
  /** Outputs Arrange said need a re-render, though the list read showed objects. */
  const [flagged, setFlagged] = useState<string[]>([])
  const outputs = given.map((output) => refreshed[output.id] ?? output)
  const unusable = outputs.filter((output) => needsBackfill(output) || flagged.includes(output.id))
  const rows = rowsOf(outputs.filter((output) => !unusable.includes(output)))
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [goal, setGoal] = useState<ArrangeGoal>('fewest_plates')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [asking, setAsking] = useState(false)
  const [backfill, setBackfill] = useState<Record<string, string>>({})
  const [failures, setFailures] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const countOf = (key: string, fallback: number) => counts[key] ?? fallback

  /** The arrange in flight: closing the dialog (or leaving the page) stops its wait. */
  const running = useRef<AbortController | null>(null)
  useEffect(() => {
    if (open) {
      // Opened again (History keeps it mounted): nothing from the last time shows.
      setAsking(false)
      setFailures(null)
      setError(null)
      return
    }
    running.current?.abort()
    running.current = null
  }, [open])
  useEffect(() => () => running.current?.abort(), [])

  /** Arrange: first re-render `stale` (confirmed by the user), then arrange every output that can be. */
  async function submit(stale: Output[]) {
    setBusy(true)
    setAsking(false)
    setError(null)
    setFailures(null)
    const controller = new AbortController()
    running.current = controller
    try {
      let usable = outputs.filter((output) => !unusable.includes(output))
      let skipped: string | undefined
      if (stale.length > 0) {
        const { ready, failed } = await backfillOutputs(stale, {
          signal: controller.signal,
          onProgress: (output, message) => setBackfill((shown) => ({ ...shown, [output.id]: message })),
        })
        setRefreshed((known) => ({ ...known, ...Object.fromEntries(ready.map((o) => [o.id, o])) }))
        setFlagged((ids) => ids.filter((id) => !ready.some((o) => o.id === id)))
        if (failed.length > 0) skipped = backfillFailures(failed)
        // Every output that can be arranged is, the ones that never needed a re-render too.
        if (ready.length === 0 && usable.length === 0) {
          setFailures(`${skipped} Nothing was arranged.`)
          return
        }
        if (skipped) setFailures(skipped)
        // In the dialog's order, each re-rendered output in its old one's place.
        const before = usable
        usable = outputs.flatMap((output) =>
          before.includes(output) ? [output] : ready.filter((o) => o.id === output.id),
        )
      }
      const body: ArrangeRequest = {
        objects: rowsOf(usable).map(({ output, object, key }) => ({
          output_id: output.id,
          part: object.part,
          count: countOf(key, object.count),
        })),
        goal,
        name: name.trim() || null,
      }
      const arranged = await runArrange(slug, body, { onProgress: setProgress, signal: controller.signal })
      // Closed while the output was being saved: it is saved, a normal output in
      // History, but this dialog no longer acts on it.
      if (controller.signal.aborted) return
      onArranged(skipped ? { ...arranged, skipped } : arranged)
    } catch (cause) {
      if (controller.signal.aborted) return
      const ids = backfillIds(cause)
      if (ids) {
        setFlagged((known) => [...new Set([...known, ...ids])])
        setAsking(true)
        return
      }
      setError(cause instanceof ApiError ? cause.detail : (cause as Error).message)
    } finally {
      if (running.current === controller) running.current = null
      setBusy(false)
      setProgress(null)
      setBackfill({})
    }
  }

  return (
    <Dialog open={open} title="Arrange" onClose={onClose}>
      <div className="flex flex-col gap-3">
        {unusable.length > 0 && (
          <p role="status" className="text-[12px] text-muted">
            {backfillNote(unusable)}
          </p>
        )}
        <ul className="flex flex-col gap-1.5">
          {rows.map(({ output, object, key }) => {
            const label = `${object.bom_piece ?? object.file} — ${output.name ?? output.id}`
            return (
              <li key={key} className="flex items-center justify-between gap-2 text-[13px]">
                <span>{label}</span>
                <input
                  type="number"
                  min={0}
                  max={500}
                  aria-label={`Copies of ${label}`}
                  value={countOf(key, object.count)}
                  onChange={(event) =>
                    setCounts({ ...counts, [key]: Math.max(0, Number(event.target.value) || 0) })
                  }
                  className="sb-field sb-num w-20"
                />
              </li>
            )
          })}
        </ul>
        <label htmlFor="arrange-goal" className="text-[12px] text-muted">
          Goal
        </label>
        <select
          id="arrange-goal"
          value={goal}
          onChange={(event) => setGoal(event.target.value as ArrangeGoal)}
          className="sb-field"
        >
          {Object.entries(GOAL_LABELS).map(([value, text]) => (
            <option key={value} value={value}>
              {text}
            </option>
          ))}
        </select>
        <label htmlFor="arrange-name" className="text-[12px] text-muted">
          Name
        </label>
        <input
          id="arrange-name"
          value={name}
          onChange={(event) => setName(event.target.value)}
          className="sb-field"
        />
        <BackfillProgress outputs={outputs} progress={backfill} />
        <p aria-live="polite" className="text-[12px] text-faint">
          {progress}
        </p>
        {(failures || error) && (
          <p role="alert" className="text-[13px] text-warn">
            {[failures, error].filter(Boolean).join(' ')}
          </p>
        )}
        {asking && (
          <BackfillPrompt outputs={unusable} onConfirm={() => void submit(unusable)} onCancel={() => setAsking(false)} />
        )}
        <Button
          onClick={() => (unusable.length > 0 ? setAsking(true) : void submit([]))}
          disabled={busy || asking || (rows.length === 0 && unusable.length === 0)}
          aria-busy={busy}
        >
          Arrange
        </Button>
      </div>
    </Dialog>
  )
}
