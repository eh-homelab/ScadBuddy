import { useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { ArrangeRequest, LibraryEntry, Output } from '../api/types'
import {
  backfillFailures,
  backfillIds,
  backfillNote,
  backfillOutputs,
  fromFiles,
  fromOutputs,
  GOAL_LABELS,
  needsBackfill,
  runArrange,
  sourceKey,
  type ArrangeGoal,
  type ArrangeSource,
  type Arranged,
} from '../lib/arrange'
import { AddFiles, AddFromModel } from './ArrangeSourcePicker'
import { BackfillProgress, BackfillPrompt } from './BackfillPrompt'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'

type Props = {
  open: boolean
  /** #1864 — outputs of any template and library files, in any mix. */
  sources: ArrangeSource[]
  onClose: () => void
  onArranged: (arranged: Arranged) => void
}

/** Each object of each output that has them, as a row with its key. */
function rowsOf(outputs: Output[]) {
  return outputs.flatMap((output) =>
    (output.manifest ?? []).map((object) => ({ output, object, key: `${output.id}:${object.part}` })),
  )
}

/** "A", "A and B", "A, B and C". */
function listed(names: string[]): string {
  if (names.length <= 1) return names[0] ?? ''
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

/** Why these library files are left out, in one sentence (#1863 reads them). */
function plainNote(files: LibraryEntry[]): string {
  const one = files.length === 1
  return `${listed(files.map((f) => f.filename))} ${one ? 'was' : 'were'} not made by ScadBuddy, so ${one ? 'it' : 'they'} cannot be arranged yet.`
}

/** The templates of `outputs`, first seen first: what the result can be filed under. */
function templatesOf(outputs: Output[]): string[] {
  return [...new Set(outputs.map((output) => output.slug))]
}

/**
 * #314 — objects from several outputs onto shared plates (spec 2026-09-27 §7). Each
 * object of each source is a row with its copies; Arrange lays them out for the goal on
 * the configured printer's plate, with no re-render, and saves the result. #1864 — the
 * sources mix outputs of any template and library files: a file ScadBuddy uploaded
 * arranges through the output it is a copy of, any other is shown as not arrangeable
 * yet. Add files and Add from a model add more before the run. An output saved before
 * Arrange has no objects: Arrange asks to re-render it first, then arranges with it (#902).
 */
export function ArrangeDialog({ open, sources: given, onClose, onArranged }: Props) {
  /** Sources added inside the dialog, after the ones it was opened with. */
  const [added, setAdded] = useState<ArrangeSource[]>([])
  const [adding, setAdding] = useState<'files' | 'model' | null>(null)
  /** The output behind each library file, read by id; null when it could not be read. */
  const [read, setRead] = useState<Record<string, Output | null>>({})
  /** Outputs re-rendered here, read back with their objects. */
  const [refreshed, setRefreshed] = useState<Record<string, Output>>({})
  /** Outputs Arrange said need a re-render, though the list read showed objects. */
  const [flagged, setFlagged] = useState<string[]>([])
  const [filing, setFiling] = useState<string | null>(null)

  const keys = new Set<string>()
  const sources = [...given, ...added].filter((source) => {
    const key = sourceKey(source)
    if (keys.has(key)) return false
    keys.add(key)
    return true
  })
  const named = new Map(sources.flatMap((s) => (s.kind === 'output' ? [[s.output.id, s.output] as const] : [])))
  const plain: LibraryEntry[] = []
  const reading: LibraryEntry[] = []
  const behind: Output[] = []
  for (const source of sources) {
    if (source.kind === 'output') {
      behind.push(source.output)
      continue
    }
    const id = source.file.output_id
    const output = id ? (named.get(id) ?? read[id]) : null
    if (output === undefined) reading.push(source.file)
    else if (output === null) plain.push(source.file)
    else behind.push(output)
  }
  // An output reached as itself and through its library file is arranged once.
  const outputs = [...new Map(behind.map((o) => [o.id, refreshed[o.id] ?? o])).values()]
  const unusable = outputs.filter((output) => needsBackfill(output) || flagged.includes(output.id))
  const rows = rowsOf(outputs.filter((output) => !unusable.includes(output)))
  const templates = templatesOf(outputs.filter((output) => !unusable.includes(output)))
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

  // Read the output behind each library file once, whoever added it.
  const unread = [
    ...new Set(
      sources.flatMap((s) =>
        s.kind === 'library' && s.file.output_id && !named.has(s.file.output_id) && !(s.file.output_id in read)
          ? [s.file.output_id]
          : [],
      ),
    ),
  ].join(',')
  useEffect(() => {
    if (!open || !unread) return
    let live = true
    for (const id of unread.split(',')) {
      api
        .getOutput(id)
        .then((output) => live && setRead((known) => ({ ...known, [id]: output })))
        .catch(() => live && setRead((known) => ({ ...known, [id]: null })))
    }
    return () => {
      live = false
    }
  }, [open, unread])

  /** The arrange in flight: closing the dialog (or leaving the page) stops its wait. */
  const running = useRef<AbortController | null>(null)
  useEffect(() => {
    if (open) {
      // Opened again (History keeps it mounted): nothing from the last time shows.
      setAsking(false)
      setFailures(null)
      setError(null)
      setAdded([])
      setAdding(null)
      setFiling(null)
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
        const why = backfillFailures(failed)
        // Every output that can be arranged is, the ones that never needed a re-render too.
        if (ready.length === 0 && usable.length === 0) {
          setFailures(`${why} Nothing was arranged.`)
          return
        }
        // Said wherever this lands, History too: the arrange went ahead without them (#1007).
        if (failed.length > 0) {
          skipped = `${why} ${failed.length === 1 ? 'It was' : 'They were'} left out of the arrange.`
        }
        if (skipped) setFailures(skipped)
        // In the dialog's order, each re-rendered output in its old one's place.
        const before = usable
        usable = outputs.flatMap((output) =>
          before.includes(output) ? [output] : ready.filter((o) => o.id === output.id),
        )
      }
      // The chosen template, unless every output of it was left out: then the first one's.
      const kept = templatesOf(usable)
      const slug = filing && kept.includes(filing) ? filing : kept[0]
      if (!slug) throw new Error('Nothing to arrange.')
      const body: ArrangeRequest = {
        objects: rowsOf(usable).map(({ output, object, key }) => ({
          output_id: output.id,
          part: object.part,
          count: countOf(key, object.count),
        })),
        goal,
        name: name.trim() || null,
        slug,
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
        {plain.length > 0 && (
          <p role="status" className="text-[12px] text-muted">
            {plainNote(plain)}
          </p>
        )}
        {reading.length > 0 && (
          <p className="text-[12px] text-faint">{`Reading ${listed(reading.map((f) => f.filename))}…`}</p>
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
        {adding === null && (
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => setAdding('files')} disabled={busy}>
              Add files
            </Button>
            <Button size="sm" onClick={() => setAdding('model')} disabled={busy}>
              Add from a model
            </Button>
          </div>
        )}
        {adding === 'files' && (
          <AddFiles
            chosen={(file) => keys.has(`library:${file.id}`)}
            onAdd={(files) => {
              setAdded((now) => [...now, ...fromFiles(files)])
              setAdding(null)
            }}
            onCancel={() => setAdding(null)}
          />
        )}
        {adding === 'model' && (
          <AddFromModel
            chosen={(output) => outputs.some((o) => o.id === output.id)}
            onAdd={(picked) => {
              setAdded((now) => [...now, ...fromOutputs(picked)])
              setAdding(null)
            }}
            onCancel={() => setAdding(null)}
          />
        )}
        {templates.length > 1 && (
          <>
            <label htmlFor="arrange-filing" className="text-[12px] text-muted">
              File under
            </label>
            <select
              id="arrange-filing"
              value={filing && templates.includes(filing) ? filing : templates[0]}
              onChange={(event) => setFiling(event.target.value)}
              className="sb-field"
            >
              {templates.map((slug) => (
                <option key={slug} value={slug}>
                  {slug}
                </option>
              ))}
            </select>
          </>
        )}
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
          disabled={busy || asking || reading.length > 0 || (rows.length === 0 && unusable.length === 0)}
          aria-busy={busy}
        >
          Arrange
        </Button>
      </div>
    </Dialog>
  )
}
