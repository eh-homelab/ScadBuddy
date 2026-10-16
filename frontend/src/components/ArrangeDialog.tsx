import { useEffect, useRef, useState } from 'react'
import { api, ApiError, mayHaveRun, newRequestId } from '../api/client'
import type {
  ArrangeObject,
  ArrangeRequest,
  LibraryEntry,
  LibraryFileObjects,
  ModelSummary,
  Output,
} from '../api/types'
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
  type OutputRef,
  objectNames,
} from '../lib/arrange'
import { shortId } from '../lib/format'
import { traceAction, type Within } from '../lib/traceAction'
import { AddFiles, AddFromModel } from './ArrangeSourcePicker'
import { BackfillProgress, BackfillPrompt } from './BackfillPrompt'
import { CopyColours } from './CopyColours'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Spinner } from './ui/Spinner'

type Props = {
  open: boolean
  /** #1864 — outputs of any template and library files, in any mix. */
  sources: ArrangeSource[]
  onClose: () => void
  onArranged: (arranged: Arranged) => void
}

/**
 * Each object of each output that has them, as a row with its key. #3637 — an arranged
 * output may hold one part twice, once in other colours: each colouring is a row.
 */
function rowsOf(outputs: Output[]) {
  return outputs.flatMap((output) =>
    (output.manifest ?? []).map((object) => ({
      output,
      object,
      key: `${output.id}:${object.part}:${JSON.stringify(object.recolour ?? {})}`,
    })),
  )
}

/** #3638 — what "File under" offers for a new model made from the arranged library file. */
const NEW_HOME = ':new-model'

/** A library file's name without its extension: the new model's name (#3638). */
function stemOf(filename: string): string {
  return filename.replace(/\.(3mf|stl)$/i, '').trim()
}

/**
 * What a model named `name` is filed as: a copy of the backend's
 * `scadbuddy/library/slugs.py` `slugify` (lower case, each run of other characters one
 * `-`, none at the ends). Change both together; `ArrangeDialog.test.tsx` pins a case.
 */
function slugOf(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '')
}

/** `base`, or `base 2`, `base 3`… */
function numbered(base: string, n: number): string {
  return n === 1 ? base : `${base} ${n}`
}

/** The name to try (`stem`, else `fallback`) and the first number whose slug no model has. */
function freeName(stem: string, models: Pick<ModelSummary, 'slug'>[], fallback: string): { base: string; n: number } {
  const base = slugOf(stem) ? stem : fallback
  const taken = new Set(models.map((model) => model.slug))
  let n = 1
  while (taken.has(slugOf(numbered(base, n)))) n += 1
  return { base, n }
}

/** #3637 — a copy added to a row, in other colours. */
type Variant = { key: string; base: string }

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

/** A file's name without its extension: "bookmark.3mf" is "bookmark". A sliced file
 * never reaches Arrange (the Library page leaves them out), so no `.gcode.3mf` case. */
function stem(filename: string): string {
  const cut = filename.lastIndexOf('.')
  return cut > 0 ? filename.slice(0, cut) : filename
}

/** The longest name the server takes (`ArrangeRequest.name`, `max_length=200`). */
export const MAX_ARRANGE_NAME = 200

/**
 * "Arrange of A, B and C"; the first three and a count past that; empty with none. Never
 * longer than the server takes: fewer names are shown, the rest counted, until it fits,
 * and a single name too long on its own is cut with an ellipsis. Nothing the user typed
 * made it, so it must never be the reason the arrange is refused.
 */
function arrangeName(names: string[]): string {
  const distinct = [...new Set(names.filter(Boolean))]
  if (distinct.length === 0) return ''
  for (let count = Math.min(3, distinct.length); count >= 1; count--) {
    const rest = distinct.length - count
    const shown = rest > 0 ? [...distinct.slice(0, count), `${rest} more`] : distinct.slice(0, count)
    const name = `Arrange of ${shown.join(', ')}`
    if (name.length <= MAX_ARRANGE_NAME) return name
  }
  const rest = distinct.length - 1
  const tail = rest > 0 ? `, ${rest} more` : ''
  const room = MAX_ARRANGE_NAME - 'Arrange of '.length - tail.length - 1
  return `Arrange of ${distinct[0]!.slice(0, room)}…${tail}`
}

/** The templates of `outputs`, first seen first: what the result can be filed under. */
function templatesOf(outputs: Output[]): string[] {
  return [...new Set(outputs.map((output) => output.slug))]
}

/** The outputs with at least one row not removed: an output whose every row was removed
 * sends nothing, so its template is not one the arrange can be filed under. */
function withRows(outputs: Output[], removed: string[]): Output[] {
  return outputs.filter((output) => rowsOf([output]).some((row) => !removed.includes(row.key)))
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
  /** #3638 — the model list could not be read: whether the file has a model is unknown. */
  const [modelsFailed, setModelsFailed] = useState(false)
  /** The same, for the open effect, which runs on `open` alone. */
  const modelsFailedRef = useRef(false)
  modelsFailedRef.current = modelsFailed
  /** Outputs re-rendered here, read back with their objects. */
  const [refreshed, setRefreshed] = useState<Record<string, Output>>({})
  /** Outputs Arrange said need a re-render, though the list read showed objects. */
  const [flagged, setFlagged] = useState<string[]>([])
  const [filing, setFiling] = useState<string | null>(null)
  /** #2286 — rows taken out of this arrange with their Remove button, by row key. Kept
   * across a re-render (#902): a re-rendered output keeps its id and its parts, so a row
   * removed before "Re-render again" stays out once the re-render returns. */
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
  /** #2285 — the file each output was added as: what the user picked it by. */
  const viaFile = new Map<string, string>()
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
    else {
      behind.push(output)
      viaFile.set(output.id, source.file.filename)
    }
  }
  // An output reached as itself and through its library file is arranged once.
  const outputs = [...new Map(behind.map((o) => [o.id, refreshed[o.id] ?? o])).values()]
  /** Outputs with no objects to arrange until they are re-rendered (#902). */
  const stale = outputs.filter((output) => needsBackfill(output) || flagged.includes(output.id))
  /** #2285 — those still in this arrange: listed as rows of their own, with Remove. */
  const unusable = stale.filter((output) => !removed.includes(`output:${output.id}`))
  const rows = rowsOf(outputs.filter((output) => !stale.includes(output))).filter((row) => !removed.includes(row.key))
  const fileRows = fileRowsOf(plain).filter((row) => !removed.includes(row.key))
  const ownTemplates = templatesOf(withRows(outputs.filter((output) => !stale.includes(output)), removed))
  // A library file ScadBuddy did not make belongs to no template: with one in the mix the
  // result may be filed under any template the user picks, its outputs' first (#2284).
  const anyTemplate = fileRows.length > 0
  // #3638 — library files alone are no model's: filed by default under the first file's
  // own model, the one made from it before or a new one, never an unrelated template.
  const homeFile = ownTemplates.length === 0 ? fileRows[0]?.file : undefined
  const home = homeFile
    ? models?.find((model) => model.mesh_source?.kind === 'bambuddy_library' && model.mesh_source.file_id === homeFile.id)
    : undefined
  const templates = anyTemplate
    ? [
        ...new Set([
          // Never a new model when the list failed: the file may have one already.
          ...(homeFile && (home || !modelsFailed) ? [home?.slug ?? NEW_HOME] : []),
          ...ownTemplates,
          ...(models ?? []).map((model) => model.slug),
        ]),
      ]
    : ownTemplates
  /** A template by its name, as "File under" says it (#2284); its slug until the list is read. */
  const modelName = (slug: string) =>
    slug === NEW_HOME && homeFile
      ? `A new model: ${stemOf(homeFile.filename) || homeFile.filename}`
      : (models?.find((model) => model.slug === slug)?.name ?? slug)
  /** Each output's object names, worked out once per render rather than once per row. */
  const names = new Map<string, Map<string, string>>()
  const namesOf = (output: Output) => {
    let found = names.get(output.id)
    if (!found) names.set(output.id, (found = objectNames(output, modelName)))
    return found
  }
  /** #2285 — an output by its name, else the file it was added as, else its short id. */
  const outputName = (output: Output) => output.name ?? viaFile.get(output.id) ?? shortId(output.id)
  /** #2285 — an output as a note or a prompt names it: never by its raw 32-hex id. */
  const refOf = (output: Output): OutputRef => ({
    id: output.id,
    name: output.name ?? viaFile.get(output.id) ?? `${modelName(output.slug)} ${shortId(output.id)}`,
  })
  /**
   * #2286 — what an unnamed arrange is saved as: "Arrange of Lemon, Bag clip", by the
   * models and files it holds, rather than a hash-like id. Only once the model list has
   * answered: a slug is not a name (#2541). `named` are the outputs it holds: before
   * Arrange, every one shown (a stale one is about to be re-rendered); at the save, only
   * the ones actually arranged, so one whose re-render failed is not in the name.
   */
  const nameOf = (named: Output[]) =>
    arrangeName([
      ...(models && models.length > 0 ? templatesOf(named).map((slug) => modelName(slug)) : []),
      // A file whose every row was removed is not in the arrange, so not in its name.
      ...plain
        .filter(({ file, read }) => fileRowsOf([{ file, read }]).some((row) => !removed.includes(row.key)))
        .map(({ file }) => stem(file.filename)),
    ])
  const defaultName = nameOf(outputs.filter((o) => unusable.includes(o) || withRows([o], removed).length > 0))
  const [counts, setCounts] = useState<Record<string, number>>({})
  /** #3637 — each row's colours swapped, by row key; a row not here prints as its source. */
  const [swaps, setSwaps] = useState<Record<string, Record<string, string>>>({})
  /** #3637 — copies added in other colours, each under the row it copies. */
  const [variants, setVariants] = useState<Variant[]>([])
  /** #3638 — the model this press made for the file, named when a later step fails. */
  const madeHome = useRef<string | null>(null)
  /** The row whose colour editor is open. */
  const [editing, setEditing] = useState<string | null>(null)
  const [goal, setGoal] = useState<ArrangeGoal>('fewest_plates')
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<string | null>(null)
  /**
   * #2283 — when the stage on screen began, and the clock it is read against: a stage
   * that runs long shows how long, so a slow save does not read as nothing happening.
   */
  const stage = useRef<string | null>(null)
  // In state, next to `progress`, so what render reads of it is state, not a ref.
  const [stageAt, setStageAt] = useState(0)
  const [clock, setClock] = useState(0)
  const say = (message: string | null) => {
    if (stage.current !== message) {
      stage.current = message
      setStageAt(Date.now())
    }
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
  const attempt = useRef<{ key: string; arrangeKey: string; body: string; arranged?: ArrangedJob } | null>(null)
  /** Whether the last failure is worth trying again as it is (not a refusal of the request). */
  const [retryable, setRetryable] = useState(false)

  const countOf = (key: string, fallback: number) => counts[key] ?? fallback
  const copiesOf = (base: string) => variants.filter((variant) => variant.base === base)
  /** #3637 — a copy of `base` in other colours, its editor open to pick them. */
  function addCopy(base: string) {
    const key = `${base}#${Date.now().toString(36)}${variants.length}`
    setVariants((now) => [...now, { key, base }])
    setEditing(key)
  }

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
      .then((found) => {
        if (!live) return
        setModels(found)
        setModelsFailed(false)
      })
      // #2541 — deliberate: a failed list costs only names. "File under" then offers the
      // outputs' own templates by slug, and the saved line says no name (`savedNote`).
      .catch(() => {
        if (!live) return
        setModels([])
        setModelsFailed(true)
      })
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
      setSwaps({})
      setVariants([])
      setEditing(null)
      setRemoved([])
      // #3638 — a model list that failed is read again, as a failed file read is.
      if (modelsFailedRef.current) setModels(null)
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
  const elapsed = busy && progress ? Math.max(0, Math.floor((clock - stageAt) / 1000)) : 0

  /** Read again every file or output whose read failed, as opening the dialog again does. */
  function readAgain() {
    setRead((known) => Object.fromEntries(Object.entries(known).filter(([, value]) => !('error' in value))))
    setObjects((known) => Object.fromEntries(Object.entries(known).filter(([, value]) => !('error' in value))))
  }

  /**
   * Re-render `stale` and take in what came back: a ready output stands in for its stale
   * self and is no longer flagged. Shared by a row's Re-render and Arrange's (#3553).
   */
  async function rerenderAll(stale: Output[], signal: AbortSignal, within: Within) {
    say(rerenderStage(stale.length))
    const result = await backfillOutputs(stale.map(refOf), {
      signal,
      within,
      onProgress: (ref, message) => setBackfill((shown) => ({ ...shown, [ref.id]: message })),
    })
    const { ready } = result
    setRefreshed((known) => ({ ...known, ...Object.fromEntries(ready.map((o) => [o.id, o])) }))
    setFlagged((ids) => ids.filter((id) => !ready.some((o) => o.id === id)))
    return result
  }

  /** #2285 — re-render one output from its row, without arranging yet. */
  async function rerender(output: Output) {
    setBusy(true)
    setAsking(false)
    setError(null)
    setFailures(null)
    const controller = new AbortController()
    running.current = controller
    try {
      // One action, as Arrange's re-renders are (#2292): its start is the action's child.
      const { failed } = await traceAction('rerender', { 'scadbuddy.output_id': output.id }, (within) =>
        rerenderAll([output], controller.signal, within),
      )
      if (failed.length > 0) setFailures(backfillFailures(failed))
    } catch (cause) {
      if (controller.signal.aborted) return
      setError(cause instanceof ApiError ? cause.detail : (cause as Error).message)
      setRetryable(false)
    } finally {
      if (running.current === controller) running.current = null
      setBusy(false)
      say(null)
      setBackfill({})
    }
  }

  /**
   * Arrange: first re-render `rerendering` (confirmed by the user), then arrange every
   * output that can be. Never the dialog's `stale`: a stale output the user removed is in
   * neither, and must not come back into the request (#2754 review).
   */
  async function submit(rerendering: Output[]) {
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
          'scadbuddy.rerendered': rerendering.length,
        },
        async (within, span) => {
          // Every output that needs no re-render; a stale one comes back only once re-rendered.
          let usable = outputs.filter((output) => !stale.includes(output))
          let skipped: string | undefined
          if (rerendering.length > 0) {
            const { ready, failed } = await rerenderAll(rerendering, controller.signal, within)
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
          const kept = templatesOf(withRows(usable, removed))
          const choices = anyTemplate ? [...new Set([...kept, ...templates])] : kept
          let slug = filing && choices.includes(filing) ? filing : choices[0]
          if (!slug) throw new Error('Nothing to arrange.')
          let made: ModelSummary | undefined
          madeHome.current = null
          if (slug === NEW_HOME && homeFile) {
            // #3638 — the file's own model, made once: arranging it again files there too.
            setProgress(`Making a model for ${homeFile.filename}…`)
            // The slug is guessed here; a model the list did not show (or a name the
            // guess gets wrong) is a 409, and the next free name is tried.
            const first = freeName(stemOf(homeFile.filename), models ?? [], `Library file ${homeFile.id}`)
            for (let attempt = 0; made === undefined; attempt += 1) {
              // Refused as taken (a model the list did not show): the server's own check,
              // so the next try numbers the name on rather than guessing slugs again.
              const name = numbered(first.base, first.n + attempt)
              try {
                made = await within(() =>
                  api.createMeshModel({
                    name,
                    description: `Made from ${homeFile.filename} in the Bambuddy library, to keep its arranged plates.`,
                    library_file_id: homeFile.id,
                  }),
                )
              } catch (cause) {
                if (!(cause instanceof ApiError && cause.status === 409) || attempt >= 4) throw cause
              }
            }
            const added = made
            madeHome.current = added.name
            setModels((known) => [...(known ?? []), added])
            setFiling(added.slug)
            slug = added.slug
          }
          const objects = (rowsFor: typeof rows): ArrangeObject[] =>
            rowsFor.flatMap(({ output, object, key }) => {
              const own = { output_id: output.id, part: object.part }
              const recolour = swaps[key] ?? object.recolour ?? {}
              // A part the output holds twice, in two colourings, is named by its colours.
              const twice = (output.manifest ?? []).filter((entry) => entry.part === object.part).length > 1
              return [
                {
                  ...own,
                  count: countOf(key, object.count),
                  // Sent whenever it was touched, or the entry has one: omitted, the
                  // API keeps the entry's, so a colour put back would print swapped.
                  ...(twice || key in swaps || Object.keys(object.recolour ?? {}).length > 0
                    ? { recolour }
                    : {}),
                },
                ...copiesOf(key).map((copy) => ({ ...own, count: countOf(copy.key, 1), recolour: swaps[copy.key] ?? {} })),
              ]
            })
          const body: ArrangeRequest = {
            objects: [
              ...objects(rowsOf(usable).filter((row) => !removed.includes(row.key))),
              ...fileRows.flatMap(({ file, object, key }) => {
                const own = { library_file_id: file.id, part: object.part }
                const swapped = swaps[key]
                return [
                  {
                    ...own,
                    count: countOf(key, object.count),
                    ...(swapped && Object.keys(swapped).length > 0 ? { recolour: swapped } : {}),
                  },
                  ...copiesOf(key).map((copy) => ({ ...own, count: countOf(copy.key, 1), recolour: swaps[copy.key] ?? {} })),
                ]
              }),
            ],
            goal,
            name: name.trim() || nameOf(withRows(usable, removed)) || null,
            slug,
          }
          // Keyed by what the user chose, not by the generated name: that changes once the
          // model list answers, and a "Try again" must keep its keys (#2749 review). The
          // server joins a key to the start or save it already began (`run_operation`), so
          // if the first attempt did start, its job and its name, generated or blank, are
          // the ones kept.
          const signature = JSON.stringify({ ...body, name: name.trim() || null })
          if (attempt.current?.body !== signature) {
            attempt.current = { key: newRequestId(), arrangeKey: newRequestId(), body: signature }
          }
          const current = attempt.current
          const arranged = await runArrange(slug, body, {
            onProgress: say,
            within,
            signal: controller.signal,
            saveKey: current.key,
            arrangeKey: current.arrangeKey,
            arranged: current.arranged,
            onArranged: (job) => (current.arranged = job),
          })
          attempt.current = null
          // Closed while the output was being saved: it is saved, a normal output in
          // History, but this dialog no longer acts on it.
          if (controller.signal.aborted) return
          // Only a name the model list gave: a slug is not a name (#2541).
          const filedUnder = [...(models ?? []), ...(made ? [made] : [])].find(
            (model) => model.slug === arranged.output.slug,
          )?.name
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
      const why = cause instanceof ApiError ? cause.detail : (cause as Error).message
      // #3638 — a model made for the file stays: the next Arrange files there.
      const kept = madeHome.current
      setError(kept ? `${why} The model ${kept} was made for this file and is kept.` : why)
      // The save's key is kept only when its save may have started (no answer, or a start
      // the server may hold): then the same key follows it. A definite failure is recorded
      // under its key and would be answered again as it is, so the next press gets a new
      // one; the finished job is kept, since nothing of it was saved.
      if (attempt.current && !mayHaveRun(cause)) {
        attempt.current.key = newRequestId()
        attempt.current.arrangeKey = newRequestId()
      }
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
      onOpenChange={(next) => {
        if (!next) onClose()
      }}
    >
      <DialogContent showCloseButton={false} className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 p-0 sm:max-w-lg">
        <DialogHeader className="shrink-0 border-b px-5 py-4">
          <DialogTitle>Arrange</DialogTitle>
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
      <div className="flex flex-col gap-3">
        {unusable.length > 0 && (
          <p role="status" className="text-[12px] text-muted">
            {backfillNote(unusable.map(refOf))}
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
          <Button type="button" variant="outline" size="sm" className="self-start" onClick={readAgain} disabled={busy}>
            Read them again
          </Button>
        )}
        {reading.length > 0 && (
          <p className="text-[12px] text-faint">{`Reading ${listed(reading.map((f) => f.filename))}…`}</p>
        )}
        <ul className="flex flex-col gap-2.5">
          {[
            ...rows.map(({ output, object, key }) => ({
              key,
              label: `${namesOf(output).get(object.part) ?? object.bom_piece ?? object.file}${
                Object.keys(object.recolour ?? {}).length > 0 ? ' (other colours)' : ''
              } — ${outputName(output)}`,
              count: object.count,
              colours: object.colours,
              recolour: swaps[key] ?? object.recolour ?? {},
            })),
            ...fileRows.map(({ file, object, key }) => ({
              key,
              label: `${object.name} — ${file.filename}`,
              count: object.count,
              colours: object.colours,
              recolour: swaps[key] ?? {},
            })),
          ].map((row) => (
            <li key={row.key} className="flex flex-col gap-1.5">
              <ArrangeRow
                label={row.label}
                count={countOf(row.key, row.count)}
                onCount={(next) => setCounts({ ...counts, [row.key]: next })}
                colours={row.colours}
                recolour={row.recolour}
                editing={editing === row.key}
                onEdit={() => setEditing(editing === row.key ? null : row.key)}
                onRecolour={(next) => setSwaps({ ...swaps, [row.key]: next })}
                // #2286 — out of this arrange, without setting its copies to 0.
                onRemove={() => setRemoved((now) => [...now, row.key])}
                locked={busy || asking}
              />
              {copiesOf(row.key).map((copy, index) => {
                const label = `${row.label}, copy ${index + 1} in other colours`
                return (
                  <div key={copy.key} className="ml-4 border-l-2 border-line pl-3">
                    <ArrangeRow
                      label={label}
                      count={countOf(copy.key, 1)}
                      onCount={(next) => setCounts({ ...counts, [copy.key]: next })}
                      colours={row.colours}
                      recolour={swaps[copy.key] ?? {}}
                      editing={editing === copy.key}
                      onEdit={() => setEditing(editing === copy.key ? null : copy.key)}
                      onRecolour={(next) => setSwaps({ ...swaps, [copy.key]: next })}
                      onRemove={() => setVariants((now) => now.filter((v) => v.key !== copy.key))}
                      locked={busy || asking}
                    />
                  </div>
                )
              })}
              {row.colours.length > 0 && (
                <Button
                  type="button"
                  variant="link"
                  size="xs"
                  onClick={() => addCopy(row.key)}
                  disabled={busy || asking}
                  aria-label={`Add a copy of ${row.label} in other colours`}
                  className="self-start px-0"
                >
                  + Add a copy in other colours
                </Button>
              )}
            </li>
          ))}
          {/* #2285 — an output that needs a re-render is listed too, not only named in the note. */}
          {unusable.map((output) => {
            const label = `${modelName(output.slug)} — ${outputName(output)}`
            return (
              <li key={`output:${output.id}`} className="flex items-center justify-between gap-2 text-[13px]">
                <span className="min-w-0 flex-1">
                  {label} <span className="text-[12px] text-warn">needs a re-render</span>
                </span>
                <Button type="button" variant="outline" size="sm" aria-label={`Re-render ${label}`} disabled={busy || asking} onClick={() => void rerender(output)}>
                  Re-render
                </Button>
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  aria-label={`Remove ${label}`}
                  disabled={busy || asking}
                  onClick={() => setRemoved((now) => [...now, `output:${output.id}`])}
                >
                  Remove
                </Button>
              </li>
            )
          })}
        </ul>
        {/* #2286 — a mis-clicked Remove is undone here, not by closing and starting over. */}
        {removed.length > 0 && (
          <Button type="button" size="sm" variant="ghost" className="self-start" onClick={() => setRemoved([])} disabled={busy || asking}>
            {`Restore ${removed.length} removed ${removed.length === 1 ? 'row' : 'rows'}`}
          </Button>
        )}
        {adding === null && (
          <div className="flex flex-wrap gap-2">
            <Button type="button" variant="outline" size="sm" onClick={() => setAdding('files')} disabled={busy || asking}>
              Add files
            </Button>
            <Button type="button" variant="outline" size="sm" onClick={() => setAdding('model')} disabled={busy || asking}>
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
        {homeFile && !home && modelsFailed && (
          <p role="status" className="text-[12px] text-warn">
            ScadBuddy could not read its models, so it cannot tell where this file's arrangements go. Close
            Arrange and open it again to retry.
          </p>
        )}
        {/* Shown whenever there is a choice, a library file in the mix too (#2284). */}
        {(templates.length > 1 || anyTemplate) && templates.length > 0 && (
          <>
            <Label htmlFor="arrange-filing" className="text-[12px] font-normal text-muted-foreground">
              File under
            </Label>
            <Select value={filing && templates.includes(filing) ? filing : templates[0]} onValueChange={setFiling}>
              <SelectTrigger id="arrange-filing" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {templates.map((slug) => (
                  <SelectItem key={slug} value={slug}>
                    {modelName(slug)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </>
        )}
        <Label htmlFor="arrange-goal" className="text-[12px] font-normal text-muted-foreground">
          Goal
        </Label>
        <Select value={goal} onValueChange={(next) => setGoal(next as ArrangeGoal)}>
          <SelectTrigger id="arrange-goal" className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(GOAL_LABELS).map(([value, text]) => (
              <SelectItem key={value} value={value}>
                {text}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Label htmlFor="arrange-name" className="text-[12px] font-normal text-muted-foreground">
          Name
        </Label>
        {/* #2286 — focus starts here, not on Add files; empty, it saves as the placeholder. */}
        <Input
          id="arrange-name"
          value={name}
          placeholder={defaultName || undefined}
          maxLength={MAX_ARRANGE_NAME}
          autoFocus
          onChange={(event) => setName(event.target.value)}
        />
        <BackfillProgress outputs={outputs.map(refOf)} progress={backfill} />
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
          <Button type="button" variant="outline" size="sm" className="self-start" onClick={() => (unusable.length > 0 ? setAsking(true) : void submit([]))}>
            Try again
          </Button>
        )}
        {/* #2286 — re-renders that failed are tried again from here, not by guessing. Straight
            to submit, with no BackfillPrompt: the user already confirmed these re-renders. */}
        {failures && !error && !busy && !asking && unusable.length > 0 && (
          <Button type="button" variant="outline" size="sm" className="self-start" onClick={() => void submit(unusable)}>
            Re-render again
          </Button>
        )}
        {asking && (
          <BackfillPrompt outputs={unusable.map(refOf)} onConfirm={() => void submit(unusable)} onCancel={() => setAsking(false)} />
        )}
      </div>
        </div>
        <DialogFooter className="m-0 shrink-0 flex-row flex-wrap items-center justify-end px-5 py-3">
              {/* #2286 — a visible way out besides Escape and the backdrop. */}
              <Button type="button" variant="ghost" onClick={onClose}>
                Close
              </Button>
              <Button
                type="button"
                onClick={() => (unusable.length > 0 ? setAsking(true) : void submit([]))}
                disabled={
                  busy ||
                  asking ||
                  reading.length > 0 ||
                  // #3638 — which model is the file's own is known only once the list is read.
                  (homeFile !== undefined && models === null) ||
                  (anyTemplate && templates.length === 0) ||
                  (rows.length === 0 && fileRows.length === 0 && unusable.length === 0)
                }
                aria-busy={busy}
              >
                Arrange
              </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** One Arrange row: what it is, how many copies, and the colours they print in (#3637). */
function ArrangeRow({
  label,
  count,
  onCount,
  colours,
  recolour,
  editing,
  onEdit,
  onRecolour,
  onRemove,
  locked = false,
}: {
  label: string
  count: number
  onCount: (next: number) => void
  colours: string[]
  recolour: Record<string, string>
  editing: boolean
  onEdit: () => void
  onRecolour: (next: Record<string, string>) => void
  onRemove?: () => void
  /** Busy or asking: Remove shows, disabled, so the row does not shift. */
  locked?: boolean
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-2 text-[13px]">
        <span>{label}</span>
        <span className="flex items-center gap-2">
          {onRemove && (
            <Button type="button" size="sm" variant="ghost" aria-label={`Remove ${label}`} disabled={locked} onClick={onRemove}>
              Remove
            </Button>
          )}
          <Input
            type="number"
            min={0}
            max={500}
            aria-label={`Copies of ${label}`}
            value={count}
            onChange={(event) => onCount(Math.max(0, Number(event.target.value) || 0))}
            className="sb-num w-20"
          />
        </span>
      </div>
      <CopyColours
        label={label}
        colours={colours}
        recolour={recolour}
        open={editing}
        onToggle={onEdit}
        onChange={onRecolour}
      />
    </div>
  )
}
