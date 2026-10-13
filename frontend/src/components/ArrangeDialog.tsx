import { useEffect, useRef, useState } from 'react'
import { api, ApiError, mayHaveRun, newRequestId } from '../api/client'
import type { ArrangeRequest, LibraryEntry, LibraryFileObjects, ModelSummary, Output } from '../api/types'
import {
  backfillFailures,
  backfillIds,
  backfillNote,
  backfillOutputs,
  fromFiles,
  fromOutputs,
  GOAL_LABELS,
  needsBackfill,
  rerenderStage,
  runArrange,
  sourceKey,
  type ArrangeGoal,
  type ArrangeSource,
  type Arranged,
  type ArrangedJob,
} from '../lib/arrange'
import { traceAction } from '../lib/traceAction'
import { AddFiles, AddFromModel } from './ArrangeSourcePicker'
import { BackfillProgress, BackfillPrompt } from './BackfillPrompt'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

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

/** #1863 — each object read from a library file ScadBuddy did not make, as a row. */
function fileRowsOf(files: { file: LibraryEntry; read: LibraryFileObjects }[]) {
  return files.flatMap(({ file, read }) =>
    read.objects.map((object) => ({ file, object, key: `library:${file.id}:${object.part}` })),
  )
}

/** A file's name without its extension: "bookmark.3mf" is "bookmark". */
function stem(filename: string): string {
  const lower = filename.toLowerCase()
  const cut = lower.endsWith('.gcode.3mf') ? filename.length - 10 : filename.lastIndexOf('.')
  return cut > 0 ? filename.slice(0, cut) : filename
}

/** "Arrange of A, B and C"; the first three and a count past that; empty with none. */
function arrangeName(names: string[]): string {
  const distinct = [...new Set(names.filter(Boolean))]
  if (distinct.length === 0) return ''
  const shown = distinct.length > 3 ? [...distinct.slice(0, 3), `${distinct.length - 3} more`] : distinct
  return `Arrange of ${shown.join(', ')}`
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
 * arranges through the output it is a copy of, and #1863 any other through the objects
 * the server reads from its 3MF; one it cannot read (a sliced file) is left out, saying
 * why. Add files and Add from a model add more before the run. An output saved before
 * Arrange has no objects: Arrange asks to re-render it first, then arranges with it (#902).
 */
export function ArrangeDialog({ open, sources: given, onClose, onArranged }: Props) {
  /** Sources added inside the dialog, after the ones it was opened with. */
  const [added, setAdded] = useState<ArrangeSource[]>([])
  const [adding, setAdding] = useState<'files' | 'model' | null>(null)
  /** The output behind each library file, read by id, or why it could not be read. */
  const [read, setRead] = useState<Record<string, Output | { error: string }>>({})
  /** #1863 — the objects of each library file ScadBuddy did not make, or why not. */
  const [objects, setObjects] = useState<Record<number, LibraryFileObjects | { error: string }>>({})
  /**
   * Every template: their names for "File under" and the saved line, and what a result
   * with a library file in it may be filed under (#2284).
   */
  const [models, setModels] = useState<ModelSummary[] | null>(null)
  /** Outputs re-rendered here, read back with their objects. */
  const [refreshed, setRefreshed] = useState<Record<string, Output>>({})
  /** Outputs Arrange said need a re-render, though the list read showed objects. */
  const [flagged, setFlagged] = useState<string[]>([])
  const [filing, setFiling] = useState<string | null>(null)
  /** #2286 — rows taken out of this arrange with their Remove button, by row key. */
  const [removed, setRemoved] = useState<string[]>([])

  const keys = new Set<string>()
  const sources = [...given, ...added].filter((source) => {
    const key = sourceKey(source)
    if (keys.has(key)) return false
    keys.add(key)
    return true
  })
  const named = new Map(sources.flatMap((s) => (s.kind === 'output' ? [[s.output.id, s.output] as const] : [])))
  const plain: { file: LibraryEntry; read: LibraryFileObjects }[] = []
  const reading: LibraryEntry[] = []
  /** Files ScadBuddy made whose output could not be read, each with why. */
  const unread: string[] = []
  /** #1863 — files whose objects could not be read from the 3MF, each with why. */
  const unreadable: string[] = []
  const behind: Output[] = []
  for (const source of sources) {
    if (source.kind === 'output') {
      behind.push(source.output)
      continue
    }
    const id = source.file.output_id
    if (!id) {
      const found = objects[source.file.id]
      if (found === undefined) reading.push(source.file)
      else if ('error' in found) unreadable.push(found.error)
      else plain.push({ file: source.file, read: found })
      continue
    }
    const output = named.get(id) ?? read[id]
    if (output === undefined) reading.push(source.file)
    else if ('error' in output) unread.push(`${source.file.filename}: ${output.error}`)
    else behind.push(output)
  }
  // An output reached as itself and through its library file is arranged once.
  const outputs = [...new Map(behind.map((o) => [o.id, refreshed[o.id] ?? o])).values()]
  const unusable = outputs.filter((output) => needsBackfill(output) || flagged.includes(output.id))
  const rows = rowsOf(outputs.filter((output) => !unusable.includes(output))).filter((row) => !removed.includes(row.key))
  const fileRows = fileRowsOf(plain).filter((row) => !removed.includes(row.key))
  const ownTemplates = templatesOf(outputs.filter((output) => !unusable.includes(output)))
  // A library file ScadBuddy did not make belongs to no template: with one in the mix the
  // result may be filed under any template the user picks, its outputs' first (#2284).
  const anyTemplate = fileRows.length > 0
  const templates = anyTemplate
    ? [...new Set([...ownTemplates, ...(models ?? []).map((model) => model.slug)])]
    : ownTemplates
  /** A template by its name, as "File under" says it (#2284); its slug until the list is read. */
  const modelName = (slug: string) => models?.find((model) => model.slug === slug)?.name ?? slug
  /**
   * #2286 — what an unnamed arrange is saved as: "Arrange of Lemon, Bag clip", by the
   * models and files it holds, rather than a hash-like id. Only once the model list has
   * answered: a slug is not a name (#2541).
   */
  const defaultName = arrangeName([
    ...(models && models.length > 0 ? templatesOf(outputs).map((slug) => modelName(slug)) : []),
    ...plain.map(({ file }) => stem(file.filename)),
  ])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [goal, setGoal] = useState<ArrangeGoal>('fewest_plates')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  /**
   * #2283 — when the stage on screen began, and the clock it is read against: a stage
   * that runs long shows how long, so a slow save does not read as nothing happening.
   */
  const stage = useRef<{ message: string | null; at: number }>({ message: null, at: 0 })
  const [clock, setClock] = useState(0)
  const say = (message: string | null) => {
    if (stage.current.message !== message) stage.current = { message, at: Date.now() }
    setProgress(message)
  }
  const [asking, setAsking] = useState(false)
  const [backfill, setBackfill] = useState<Record<string, string>>({})
  const [failures, setFailures] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  /**
   * The last arrange's save key and what it arranged. "Try again" on the same arrange
   * keeps the key, so a save that did start is followed rather than made twice; any
   * change to the arrange is a new press with a new key.
   */
  const attempt = useRef<{ key: string; body: string; arranged?: ArrangedJob } | null>(null)
  /** Whether the last failure is worth trying again as it is (not a refusal of the request). */
  const [retryable, setRetryable] = useState(false)

  const countOf = (key: string, fallback: number) => counts[key] ?? fallback

  // Read the output behind each library file once, whoever added it.
  const toRead = [
    ...new Set(
      sources.flatMap((s) =>
        s.kind === 'library' && s.file.output_id && !named.has(s.file.output_id) && !(s.file.output_id in read)
          ? [s.file.output_id]
          : [],
      ),
    ),
  ].join(',')
  // #1863 — read the objects of each file ScadBuddy did not make once, whoever added it.
  const toList = [
    ...new Set(
      sources.flatMap((s) => (s.kind === 'library' && !s.file.output_id && !(s.file.id in objects) ? [s.file.id] : [])),
    ),
  ].join(',')
  useEffect(() => {
    if (!open || !toList) return
    let live = true
    for (const id of toList.split(',').map(Number)) {
      api
        .getLibraryObjects(id)
        .then((found) => live && setObjects((known) => ({ ...known, [id]: found })))
        .catch((cause: unknown) => {
          const error = cause instanceof ApiError ? cause.detail : 'its objects could not be read'
          if (live) setObjects((known) => ({ ...known, [id]: { error } }))
        })
    }
    return () => {
      live = false
    }
  }, [open, toList])
  useEffect(() => {
    if (!open || models !== null) return
    let live = true
    api
      .listModels()
      .then((found) => live && setModels(found))
      // #2541 — deliberate: a failed list costs only names. "File under" then offers the
      // outputs' own templates by slug, and the saved line says no name (`savedNote`).
      .catch(() => live && setModels([]))
    return () => {
      live = false
    }
  }, [open, models])

  useEffect(() => {
    if (!open || !toRead) return
    let live = true
    for (const id of toRead.split(',')) {
      api
        .getOutput(id)
        .then((output) => live && setRead((known) => ({ ...known, [id]: output })))
        .catch((cause: unknown) => {
          const error = cause instanceof ApiError ? cause.detail : 'its output could not be read'
          if (live) setRead((known) => ({ ...known, [id]: { error } }))
        })
    }
    return () => {
      live = false
    }
  }, [open, toRead])

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
      setRemoved([])
      // A read that failed (a network error, say) is tried again.
      readAgain()
      return
    }
    running.current?.abort()
    running.current = null
  }, [open])
  useEffect(() => () => running.current?.abort(), [])
  useEffect(() => {
    if (!busy) return
    setClock(Date.now())
    const tick = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(tick)
  }, [busy])
  const elapsed = busy && progress ? Math.max(0, Math.floor((clock - stage.current.at) / 1000)) : 0

  /** Read again every file or output whose read failed, as opening the dialog again does. */
  function readAgain() {
    setRead((known) => Object.fromEntries(Object.entries(known).filter(([, value]) => !('error' in value))))
    setObjects((known) => Object.fromEntries(Object.entries(known).filter(([, value]) => !('error' in value))))
  }

  /** Arrange: first re-render `stale` (confirmed by the user), then arrange every output that can be. */
  async function submit(stale: Output[]) {
    setBusy(true)
    setAsking(false)
    setError(null)
    setFailures(null)
    const controller = new AbortController()
    running.current = controller
    try {
      // One action from the re-renders to the save (#2292): their starts and the save are
      // its children; each job's follow is a poll, and stays out of it.
      await traceAction(
        'arrange',
        {
          'scadbuddy.outputs': outputs.length,
          'scadbuddy.library_files': fileRows.length,
          'scadbuddy.rerendered': stale.length,
        },
        async (within, span) => {
          let usable = outputs.filter((output) => !unusable.includes(output))
          let skipped: string | undefined
          if (stale.length > 0) {
            say(rerenderStage(stale.length))
            const { ready, failed } = await backfillOutputs(stale, {
              signal: controller.signal,
              within,
              onProgress: (output, message) => setBackfill((shown) => ({ ...shown, [output.id]: message })),
            })
            setRefreshed((known) => ({ ...known, ...Object.fromEntries(ready.map((o) => [o.id, o])) }))
            setFlagged((ids) => ids.filter((id) => !ready.some((o) => o.id === id)))
            const why = backfillFailures(failed)
            // Every output that can be arranged is, the ones that never needed a re-render too.
            if (ready.length === 0 && usable.length === 0 && fileRows.length === 0) {
              setFailures(`${why} Nothing was arranged.`)
              // Ends without an output, and is not an ordinary arrange (#2892).
              span.setAttribute('scadbuddy.outcome', 'nothing_arranged')
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
          // With a library file in the mix, any template picked from every one (#1863, #2284).
          const kept = templatesOf(usable)
          const choices = anyTemplate ? [...new Set([...kept, ...templates])] : kept
          const slug = filing && choices.includes(filing) ? filing : choices[0]
          if (!slug) throw new Error('Nothing to arrange.')
          const body: ArrangeRequest = {
            objects: [
              ...rowsOf(usable)
                .filter((row) => !removed.includes(row.key))
                .map(({ output, object, key }) => ({
                  output_id: output.id,
                  part: object.part,
                  count: countOf(key, object.count),
                })),
              ...fileRows.map(({ file, object, key }) => ({
                library_file_id: file.id,
                part: object.part,
                count: countOf(key, object.count),
              })),
            ],
            goal,
            name: name.trim() || defaultName || null,
            slug,
          }
          const signature = JSON.stringify(body)
          if (attempt.current?.body !== signature) attempt.current = { key: newRequestId(), body: signature }
          const current = attempt.current
          const arranged = await runArrange(slug, body, {
            onProgress: say,
            within,
            signal: controller.signal,
            saveKey: current.key,
            arranged: current.arranged,
            onArranged: (job) => (current.arranged = job),
          })
          attempt.current = null
          // Closed while the output was being saved: it is saved, a normal output in
          // History, but this dialog no longer acts on it.
          if (controller.signal.aborted) return
          // Only a name the model list gave: a slug is not a name (#2541).
          const filedUnder = models?.find((model) => model.slug === arranged.output.slug)?.name
          const filed = filedUnder ? { ...arranged, filedUnder } : arranged
          span.setAttribute('scadbuddy.output_id', arranged.output.id)
          onArranged(skipped ? { ...filed, skipped } : filed)
        },
      )
    } catch (cause) {
      if (controller.signal.aborted) return
      const ids = backfillIds(cause)
      if (ids) {
        setFlagged((known) => [...new Set([...known, ...ids])])
        setAsking(true)
        return
      }
      setError(cause instanceof ApiError ? cause.detail : (cause as Error).message)
      // The save's key is kept only when its save may have started (no answer, or a start
      // the server may hold): then the same key follows it. A definite failure is recorded
      // under its key and would be answered again as it is, so the next press gets a new
      // one; the finished job is kept, since nothing of it was saved.
      if (attempt.current && !mayHaveRun(cause)) attempt.current.key = newRequestId()
      // A save the server refused (a 4xx: the job pruned, say) cannot save that job again:
      // the next press arranges anew.
      if (attempt.current && cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        attempt.current.arranged = undefined
      }
      // A request the server refused as it is (a 4xx) fails the same way again.
      setRetryable(!(cause instanceof ApiError) || cause.status === 0 || cause.status >= 500 || mayHaveRun(cause))
    } finally {
      if (running.current === controller) running.current = null
      setBusy(false)
      say(null)
      setBackfill({})
    }
  }

  return (
    <Dialog
      open={open}
      title="Arrange"
      onClose={onClose}
      footer={
        <>
          {/* #2286 — a visible way out besides Escape and the backdrop. */}
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button
            variant="primary"
            onClick={() => (unusable.length > 0 ? setAsking(true) : void submit([]))}
            disabled={
              busy ||
              asking ||
              reading.length > 0 ||
              (anyTemplate && templates.length === 0) ||
              (rows.length === 0 && fileRows.length === 0 && unusable.length === 0)
            }
            aria-busy={busy}
          >
            Arrange
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-3">
        {unusable.length > 0 && (
          <p role="status" className="text-[12px] text-muted">
            {backfillNote(unusable)}
          </p>
        )}
        {unreadable.length > 0 && (
          <p role="status" className="text-[12px] text-warn">
            {`Left out: ${unreadable.join('; ')}.`}
          </p>
        )}
        {unread.length > 0 && (
          <p role="status" className="text-[12px] text-warn">
            {`Left out, as ScadBuddy could not read what it made them from: ${unread.join('; ')}.`}
          </p>
        )}
        {(unreadable.length > 0 || unread.length > 0) && (
          <Button size="sm" className="self-start" onClick={readAgain} disabled={busy}>
            Read them again
          </Button>
        )}
        {reading.length > 0 && (
          <p className="text-[12px] text-faint">{`Reading ${listed(reading.map((f) => f.filename))}…`}</p>
        )}
        <ul className="flex flex-col gap-1.5">
          {[
            ...rows.map(({ output, object, key }) => ({
              key,
              label: `${object.bom_piece ?? object.file} — ${output.name ?? output.id}`,
              count: object.count,
            })),
            ...fileRows.map(({ file, object, key }) => ({
              key,
              label: `${object.name} — ${file.filename}`,
              count: object.count,
            })),
          ].map(({ key, label, count }) => (
            <li key={key} className="flex items-center justify-between gap-2 text-[13px]">
              <span className="min-w-0 flex-1">{label}</span>
              <input
                type="number"
                min={0}
                max={500}
                aria-label={`Copies of ${label}`}
                value={countOf(key, count)}
                onChange={(event) => setCounts({ ...counts, [key]: Math.max(0, Number(event.target.value) || 0) })}
                className="sb-field sb-num w-20"
              />
              {/* #2286 — out of this arrange, without setting its copies to 0. */}
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Remove ${label}`}
                disabled={busy}
                onClick={() => setRemoved((now) => [...now, key])}
              >
                Remove
              </Button>
            </li>
          ))}
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
        {/* Shown whenever there is a choice, a library file in the mix too (#2284). */}
        {(templates.length > 1 || anyTemplate) && templates.length > 0 && (
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
                  {modelName(slug)}
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
        {/* #2286 — focus starts here, not on Add files; empty, it saves as the placeholder. */}
        <input
          id="arrange-name"
          value={name}
          placeholder={defaultName || undefined}
          autoFocus
          onChange={(event) => setName(event.target.value)}
          className="sb-field"
        />
        <BackfillProgress outputs={outputs} progress={backfill} />
        <p className="flex items-center gap-2 text-[12px] text-faint">
          {busy && <Spinner />}
          <span aria-live="polite">{progress}</span>
          {/* Not announced: a screen reader hears the stage, not every second of it. */}
          {elapsed >= 3 && (
            <span aria-hidden="true" data-testid="arrange-elapsed" className="sb-num">
              {elapsed} s
            </span>
          )}
        </p>
        {(failures || error) && (
          <p role="alert" className="text-[13px] text-warn">
            {[failures, error].filter(Boolean).join(' ')}
          </p>
        )}
        {error && retryable && !busy && (
          <Button size="sm" className="self-start" onClick={() => (unusable.length > 0 ? setAsking(true) : void submit([]))}>
            Try again
          </Button>
        )}
        {/* #2286 — re-renders that failed are tried again from here, not by guessing. */}
        {failures && !error && !busy && !asking && unusable.length > 0 && (
          <Button size="sm" className="self-start" onClick={() => void submit(unusable)}>
            Re-render again
          </Button>
        )}
        {asking && (
          <BackfillPrompt outputs={unusable} onConfirm={() => void submit(unusable)} onCancel={() => setAsking(false)} />
        )}
      </div>
    </Dialog>
  )
}
