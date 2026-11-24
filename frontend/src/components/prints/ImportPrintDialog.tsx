import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router'
import { ApiError, api } from '../../api/client'
import type { ArchiveOutcome, AutoImport, ImportedPrint, LibraryFileSuggestion, PrintImportRequest } from '../../api/types'
import { modelPath } from '../../lib/deeplink'
import { useAsync } from '../../lib/useAsync'
import { LibraryFileChoice } from '../library/LibraryFileChoice'
import { PastPrintsPicker } from '../orders/PastPrintsPicker'
import { PrintSourceChoice, SLICED_REFUSAL, errorText, sourceChoice, sourceKey, usePrintSources } from './PrintSourceChoice'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** The print the form imports: an archive picked from the list, or an inbox print. */
type PickedPrint = Pick<ArchiveOutcome, 'id' | 'print_name'>

/** #4699 — where the dialog starts: an inbox print, and the source the watcher recorded. */
export type ImportStart = { archive: PickedPrint; recorded?: Pick<AutoImport, 'source' | 'library_file_id'> }

/**
 * #4699 — the source the print watcher recorded for an inbox print, as the radio group
 * names it. A printer file the watcher kept is a library file in the inbox folder, so it
 * is listed as that library file; the printer itself is never read for it.
 */
function recordedSource(row: Pick<AutoImport, 'source' | 'library_file_id'>): string | null {
  if (row.source === 'archive' || row.source === 'archive_source') return sourceKey({ kind: row.source })
  if ((row.source === 'library' || row.source === 'printer') && row.library_file_id != null) {
    return sourceKey({ kind: 'library', library_file_id: row.library_file_id })
  }
  return null
}

/** ScadBuddy's own library ids start here (backend `print_library/ids.py` `NATIVE_ID_BASE`). */
const NATIVE_ID_BASE = 1_000_000_000

/** The 404 of an import onto a file that is gone (backend `api/print_imports.py`, #7335). */
const LIBRARY_FILE_GONE = 'https://scadbuddy.dev/problems/print-imports/library-file-gone'

/** The live file a gone-file 404 names, if it names one. */
function replacementOf(caught: ApiError): LibraryFileSuggestion | null {
  return (caught.problem['replacement'] as LibraryFileSuggestion | null | undefined) ?? null
}

/** A library file in the library page, chosen. */
function libraryFilePath(fileId: number): string {
  return `/library?file=${fileId}`
}

/**
 * #4698 — "Import print": a Bambuddy archive that ScadBuddy did not print, linked to a
 * model so it shows on that model's Prints tab. Pick the archive (only the unlinked ones,
 * `scadbuddy_print`), then where its 3MF is (`GET /print-imports/{id}/sources`; one that
 * cannot be used says why, and the only usable one is chosen already), then what it is a
 * print of: a model that exists (the suggested ones first), a new mesh model made from the
 * 3MF, or (#6394, on ScadBuddy's own library) a library file, the files whose name matches
 * the job first. The printer's storage is read only after "Also look on the printer".
 *
 * With `start` (#4699, an Unsorted imports print) the dialog opens on that print, with
 * the source the watcher recorded chosen when it is listed and can be used.
 */
export function ImportPrintDialog({
  open,
  onOpenChange,
  onImported,
  start,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onImported?: (imported: ImportedPrint) => void
  start?: ImportStart | null
}) {
  const [archive, setArchive] = useState<PickedPrint | null>(start?.archive ?? null)
  const [done, setDone] = useState<ImportedPrint | null>(null)
  // Each open starts on the picker. An import still running when the dialog closes may
  // answer later: it reloads the history, but shows its "done" only in the open it began
  // in (#4698 review).
  const [opens, setOpens] = useState(0)
  const [wasOpen, setWasOpen] = useState(open)
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setArchive(start?.archive ?? null)
      setDone(null)
      setOpens((n) => n + 1)
    }
  }
  const live = useRef<number | null>(null)
  useEffect(() => {
    live.current = open ? opens : null
  }, [open, opens])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Import print</DialogTitle>
          <DialogDescription>
            Link a print that ScadBuddy did not start to a model or a library file. The print then shows with it.
          </DialogDescription>
        </DialogHeader>
        {done ? (
          <ImportDone done={done} />
        ) : archive === null ? (
          <PastPrintsPicker open={open} picked={null} onPick={setArchive} unlinkedOnly />
        ) : (
          <ImportForm
            key={archive.id}
            archive={archive}
            preferred={start?.recorded && archive.id === start.archive.id ? recordedSource(start.recorded) : null}
            // An inbox print (`start`) stays on its own print: no way back to the picker.
            onBack={start ? undefined : () => setArchive(null)}
            onImported={(imported) => {
              if (live.current === opens) setDone(imported)
              onImported?.(imported)
            }}
          />
        )}
        {done && (
          <DialogFooter>
            <Button onClick={() => onOpenChange(false)}>Close</Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}

function ImportDone({ done }: { done: ImportedPrint }) {
  const slug = done.slug
  const fileId = done.library_file_id
  return (
    <div className="flex flex-col gap-2 text-sm" data-testid="import-done">
      <p>
        The print is now a print of{' '}
        {slug ? (
          <Link className="underline" to={modelPath(slug)}>
            {slug}
          </Link>
        ) : fileId != null ? (
          <Link className="underline" to={libraryFilePath(fileId)}>
            library file {fileId}
          </Link>
        ) : (
          done.subject
        )}
        {done.created_model ? ', a new model made from its 3MF' : ''}.
      </p>
      <div className="flex gap-3">
        <Link className="underline" to={`/prints/${done.archive_id}`}>
          Open the print
        </Link>
        {slug && (
          <Link className="underline" to={modelPath(slug, 'prints')}>
            The model's prints
          </Link>
        )}
      </div>
    </div>
  )
}

const NEW = 'new'
const EXISTING = 'existing'
const FILE = 'file'

function ImportForm({
  archive,
  preferred,
  onBack,
  onImported,
}: {
  archive: PickedPrint
  /** The source to choose first when it is listed and can be used (`recordedSource`). */
  preferred: string | null
  onBack?: () => void
  onImported: (imported: ImportedPrint) => void
}) {
  const read = usePrintSources(archive.id)
  const { found, sources } = read
  const models = useAsync(() => api.listModels(), [])
  // #6394: a library file is a target only on ScadBuddy's own library.
  const settings = useAsync(() => api.getSettings(), [])
  const native = settings.data?.library_backend === 'native'

  const [picked, setPicked] = useState<string | null>(null)
  const recorded = preferred === null ? undefined : sources?.sources.find((s) => s.available && sourceKey(s) === preferred)
  const preselected = recorded ?? sources?.sources.find((s) => s.preselected)
  // The choice made for the person stays theirs when the printer's files arrive and
  // nothing is preselected any more (#4698 review).
  if (picked === null && preselected) setPicked(sourceKey(preselected))
  const choice = picked ?? (preselected ? sourceKey(preselected) : null)
  const source = sources?.sources.find((s) => sourceKey(s) === choice && s.available) ?? null

  const suggested = useMemo(() => sources?.suggested_models ?? [], [sources])
  const modelChoices = useMemo(() => {
    const first = new Set(suggested.map((m) => m.slug))
    const rest = (models.data ?? []).filter((m) => !first.has(m.slug)).map((m) => ({ slug: m.slug, name: m.name }))
    return [...suggested, ...rest]
  }, [suggested, models.data])
  const [target, setTarget] = useState<string | null>(null)
  const slug = target ?? suggested[0]?.slug ?? ''
  const [mode, setMode] = useState(EXISTING)
  const [name, setName] = useState(archive.print_name ?? '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // #7335: files found deleted or replaced since the list was read, and the suggestions read after.
  const [goneIds, setGoneIds] = useState<ReadonlySet<number>>(new Set())
  const [gone, setGone] = useState<{ file: LibraryFileSuggestion; replacement: LibraryFileSuggestion | null } | null>(null)
  const [freshFiles, setFreshFiles] = useState<LibraryFileSuggestion[] | null>(null)
  const suggestedFiles = useMemo(
    () => (freshFiles ?? sources?.suggested_library_files ?? []).filter((f) => !goneIds.has(f.id)),
    [freshFiles, sources, goneIds],
  )
  const [pickedFile, setPickedFile] = useState<LibraryFileSuggestion | null>(null)
  // A gone file is never swapped for the next suggestion: the person chooses again.
  const file = gone ? null : (pickedFile ?? suggestedFiles[0] ?? null)

  // The newest read of the suggestions: a reply of an older one that lands later is dropped.
  const latestRead = useRef(0)

  const markGone = useCallback(
    (lost: LibraryFileSuggestion, replacement: LibraryFileSuggestion | null) => {
      setGoneIds((ids) => new Set(ids).add(lost.id))
      setGone({ file: lost, replacement })
      setPickedFile(null)
      setError(null)
      // Without the printer: that read reaches it. A failed read leaves the list without the gone file.
      const read = ++latestRead.current
      void api.getPrintSources(archive.id).then(
        (again) => {
          if (read === latestRead.current) setFreshFiles(again.suggested_library_files)
        },
        () => undefined,
      )
    },
    [archive.id],
  )

  function chooseFile(next: LibraryFileSuggestion) {
    setPickedFile(next)
    setGone(null)
    setError(null)
  }

  // The library may change while the window is away: the chosen file is checked when it is back.
  const checked = mode === FILE && native && !busy ? file : null
  useEffect(() => {
    if (!checked) return
    let current = true
    const check = () => {
      if (document.visibilityState === 'hidden') return
      api.checkImportTarget(checked.id).catch((caught: unknown) => {
        if (current && caught instanceof ApiError && caught.problem.type === LIBRARY_FILE_GONE) {
          markGone(checked, replacementOf(caught))
        }
      })
    }
    window.addEventListener('focus', check)
    document.addEventListener('visibilitychange', check)
    return () => {
      current = false
      window.removeEventListener('focus', check)
      document.removeEventListener('visibilitychange', check)
    }
  }, [checked, markGone])

  const newBlocked = source?.sliced ?? false
  const ready =
    source !== null &&
    (mode === EXISTING ? slug !== '' : mode === FILE ? native && file !== null : name.trim() !== '' && !newBlocked)

  async function submit() {
    if (!source) return
    const target: Pick<PrintImportRequest, 'slug' | 'new_model' | 'library_file_id'> =
      mode === EXISTING
        ? { slug }
        : mode === FILE && file
          ? { library_file_id: file.id }
          : { new_model: { name: name.trim(), description: '' } }
    const body: PrintImportRequest = {
      source: sourceChoice(source),
      ...target,
    }
    setBusy(true)
    setError(null)
    try {
      onImported(await api.importPrint(archive.id, body))
    } catch (caught) {
      if (mode === FILE && file && caught instanceof ApiError && caught.problem.type === LIBRARY_FILE_GONE) {
        markGone(file, replacementOf(caught))
      } else {
        setError(errorText(caught))
      }
    } finally {
      setBusy(false)
    }
  }

  const title = archive.print_name || `Archive ${archive.id}`
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-sm font-medium" data-testid="import-archive">
          {title}
        </p>
        {onBack && (
          <Button type="button" variant="ghost" size="sm" onClick={onBack} disabled={busy}>
            Choose another print
          </Button>
        )}
      </div>
      {found.error && !sources ? (
        <p className="text-xs text-warn">Could not read where the print's file is: {errorText(found.error)}</p>
      ) : !sources ? (
        <p className="text-[13px] text-muted-foreground">Looking for the print's file…</p>
      ) : sources.linked ? (
        <p className="text-sm" data-testid="import-linked">
          This print is linked already
          {sources.linked.slug ? (
            <>
              {' '}
              to{' '}
              <Link className="underline" to={modelPath(sources.linked.slug)}>
                {sources.linked.slug}
              </Link>
            </>
          ) : sources.linked.library_file_id != null ? (
            <>
              {' '}
              to{' '}
              <Link className="underline" to={libraryFilePath(sources.linked.library_file_id)}>
                library file {sources.linked.library_file_id}
              </Link>
            </>
          ) : null}
          .{' '}
          <Link className="underline" to={`/prints/${archive.id}`}>
            Open the print
          </Link>
        </p>
      ) : (
        <>
          <PrintSourceChoice state={read} sources={sources} choice={choice} onChoose={setPicked} disabled={busy} />

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">What is it a print of?</legend>
            <RadioGroup aria-label="Print of" value={mode} onValueChange={setMode} className="gap-2">
              <label className="flex items-start gap-2 text-sm">
                <RadioGroupItem value={EXISTING} aria-label="A model that exists" className="mt-0.5" />
                <span>A model that exists</span>
              </label>
              {mode === EXISTING && (
                <div className="ml-6 flex flex-col gap-2">
                  {suggested.length > 0 && (
                    <div className="flex flex-wrap gap-1" aria-label="Suggested models">
                      {suggested.map((m) => (
                        <Button
                          key={m.slug}
                          type="button"
                          size="sm"
                          variant={m.slug === slug ? 'secondary' : 'outline'}
                          aria-pressed={m.slug === slug}
                          onClick={() => setTarget(m.slug)}
                        >
                          {m.name}
                        </Button>
                      ))}
                    </div>
                  )}
                  <Select value={slug} onValueChange={setTarget}>
                    <SelectTrigger aria-label="Model to link">
                      <SelectValue placeholder="Choose a model" />
                    </SelectTrigger>
                    <SelectContent>
                      {modelChoices.map((m) => (
                        <SelectItem key={m.slug} value={m.slug}>
                          {m.name}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              )}
              <label className="flex items-start gap-2 text-sm">
                <RadioGroupItem value={NEW} aria-label="A new model from the 3MF" className="mt-0.5" />
                <span>A new model from the 3MF</span>
              </label>
              {mode === NEW && (
                <div className="ml-6 flex flex-col gap-1">
                  <Label htmlFor="import-model-name">Name</Label>
                  <Input id="import-model-name" value={name} maxLength={200} onChange={(e) => setName(e.target.value)} />
                  {newBlocked && (
                    <span className="text-xs text-warn">
                      {SLICED_REFUSAL} Choose the slicer project or a library 3MF.
                    </span>
                  )}
                </div>
              )}
              {native && (
                <>
                  <label className="flex items-start gap-2 text-sm">
                    <RadioGroupItem value={FILE} aria-label="A file in the library" className="mt-0.5" />
                    <span>A file in the library</span>
                  </label>
                  {mode === FILE && (
                    <div className="ml-6 flex min-w-0 flex-col gap-2">
                      {gone && (
                        <Alert className="border-warn/40 bg-warn/10 text-warn">
                          <AlertDescription className="flex min-w-0 flex-col items-start gap-2 text-warn">
                            <span className="break-words">
                              <strong>{gone.file.filename}</strong> was deleted or replaced in the library.{' '}
                              {gone.replacement ? 'The same file is in the library again.' : 'Choose a file again.'}
                            </span>
                            {gone.replacement && (
                              <Button type="button" size="sm" variant="outline" onClick={() => gone.replacement && chooseFile(gone.replacement)}>
                                Use the new copy
                              </Button>
                            )}
                          </AlertDescription>
                        </Alert>
                      )}
                      <LibraryFileChoice
                        id="import-library-search"
                        suggested={suggestedFiles}
                        chosen={file}
                        onChoose={chooseFile}
                        refreshKey={goneIds.size}
                        // A run's per-print copy is never a target: its sweep trashes it (#6394 review).
                        keep={(hit) => hit.id >= NATIVE_ID_BASE && !hit.per_print && !goneIds.has(hit.id)}
                        emptyHint="No file name matches the job. Find the file by its name."
                      />
                    </div>
                  )}
                </>
              )}
            </RadioGroup>
          </fieldset>

          {error && (
            <p className="text-xs text-warn" role="alert">
              {error}
            </p>
          )}
          <DialogFooter>
            <Button type="button" onClick={() => void submit()} disabled={!ready || busy}>
              {busy ? 'Importing…' : 'Import'}
            </Button>
          </DialogFooter>
        </>
      )}
    </div>
  )
}
