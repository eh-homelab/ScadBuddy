import { useEffect, useRef, useState } from 'react'
import { ApiError } from '../api/client'
import type { ArrangeRequest, Output } from '../api/types'
import { GOAL_LABELS, runArrange, type ArrangeGoal, type Arranged } from '../lib/arrange'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'

type Props = {
  open: boolean
  slug: string
  outputs: Output[]
  onClose: () => void
  onArranged: (arranged: Arranged) => void
}

/**
 * #314 — objects from several outputs onto shared plates (spec 2026-09-27 §7). Each
 * object of each selected output is a row with its copies; Arrange lays them out for
 * the goal on the configured printer's plate, with no re-render, and saves the result.
 */
export function ArrangeDialog({ open, slug, outputs, onClose, onArranged }: Props) {
  const rows = outputs.flatMap((output) =>
    (output.manifest ?? []).map((object) => ({ output, object, key: `${output.id}:${object.part}` })),
  )
  const unusable = outputs.filter((output) => (output.manifest ?? []).length === 0)
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [goal, setGoal] = useState<ArrangeGoal>('fewest_plates')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const countOf = (key: string, fallback: number) => counts[key] ?? fallback

  /** The arrange in flight: closing the dialog (or leaving the page) stops its wait. */
  const running = useRef<AbortController | null>(null)
  useEffect(() => {
    if (open) return
    running.current?.abort()
    running.current = null
  }, [open])
  useEffect(() => () => running.current?.abort(), [])

  async function submit() {
    setBusy(true)
    setError(null)
    const body: ArrangeRequest = {
      objects: rows.map(({ output, object, key }) => ({
        output_id: output.id,
        part: object.part,
        count: countOf(key, object.count),
      })),
      goal,
      name: name.trim() || null,
    }
    const controller = new AbortController()
    running.current = controller
    try {
      const arranged = await runArrange(slug, body, { onProgress: setProgress, signal: controller.signal })
      // Closed while the output was being saved: it is saved, a normal output in
      // History, but this dialog no longer acts on it.
      if (controller.signal.aborted) return
      onArranged(arranged)
    } catch (cause) {
      if (controller.signal.aborted) return
      setError(cause instanceof ApiError ? cause.detail : (cause as Error).message)
    } finally {
      if (running.current === controller) running.current = null
      setBusy(false)
      setProgress(null)
    }
  }

  return (
    <Dialog open={open} title="Arrange" onClose={onClose}>
      <div className="flex flex-col gap-3">
        {unusable.length > 0 && (
          <p role="status" className="text-[12px] text-muted">
            {unusableNote(unusable.map((o) => o.name ?? o.id))}
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
        <p aria-live="polite" className="text-[12px] text-faint">
          {progress}
        </p>
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        <Button onClick={() => void submit()} disabled={busy || rows.length === 0} aria-busy={busy}>
          Arrange
        </Button>
      </div>
    </Dialog>
  )
}

/** "A was saved before Arrange; …", or "A, B and C were …" for several. */
function unusableNote(names: string[]): string {
  if (names.length === 1) return `${names[0]} was saved before Arrange; generate it again to arrange it.`
  const listed = `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
  return `${listed} were saved before Arrange; generate them again to arrange them.`
}
