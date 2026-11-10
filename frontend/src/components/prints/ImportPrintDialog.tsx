import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router'
import { ApiError, api } from '../../api/client'
import type { ArchiveOutcome, FoundSource, ImportedPrint, PrintImportRequest } from '../../api/types'
import { modelPath } from '../../lib/deeplink'
import { formatBytes, timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { PastPrintsPicker } from '../orders/PastPrintsPicker'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const KIND_LABEL: Record<FoundSource['kind'], string> = {
  archive: "The archive's file",
  archive_source: "The archive's slicer project",
  library: 'Library file',
  printer: "On the printer's storage",
}

/** One source as the radio group names it: its kind, and its library file or printer path. */
function sourceKey(source: Pick<FoundSource, 'kind' | 'library_file_id' | 'path'>): string {
  return `${source.kind}:${source.library_file_id ?? source.path ?? ''}`
}

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    const errors = (error.problem as { errors?: { msg?: string }[] }).errors
    return (error.status === 422 && errors?.[0]?.msg) || error.detail
  }
  return error instanceof Error ? error.message : String(error)
}

/**
 * #4698 — "Import print": a Bambuddy archive that ScadBuddy did not print, linked to a
 * model so it shows on that model's Prints tab. Pick the archive (only the unlinked ones,
 * `scadbuddy_print`), then where its 3MF is (`GET /print-imports/{id}/sources`; one that
 * cannot be used says why, and the only usable one is chosen already), then the model: one
 * that exists (the suggested ones first) or a new mesh model made from the 3MF. The
 * printer's storage is read only after "Also look on the printer".
 */
export function ImportPrintDialog({
  open,
  onOpenChange,
  onImported,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onImported?: (imported: ImportedPrint) => void
}) {
  const [archive, setArchive] = useState<ArchiveOutcome | null>(null)
  const [done, setDone] = useState<ImportedPrint | null>(null)
  useEffect(() => {
    if (!open) {
      setArchive(null)
      setDone(null)
    }
  }, [open])
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Import print</DialogTitle>
          <DialogDescription>
            Link a print that ScadBuddy did not start to a model. The print then shows on the model's Prints tab.
          </DialogDescription>
        </DialogHeader>
        {done ? (
          <div className="flex flex-col gap-2 text-sm" data-testid="import-done">
            <p>
              The print is now a print of{' '}
              <Link className="underline" to={modelPath(done.slug)}>
                {done.slug}
              </Link>
              {done.created_model ? ', a new model made from its 3MF' : ''}.
            </p>
            <div className="flex gap-3">
              <Link className="underline" to={`/prints/${done.archive_id}`}>
                Open the print
              </Link>
              <Link className="underline" to={modelPath(done.slug, 'prints')}>
                The model's prints
              </Link>
            </div>
          </div>
        ) : archive === null ? (
          <PastPrintsPicker open={open} picked={null} onPick={setArchive} unlinkedOnly />
        ) : (
          <ImportForm
            key={archive.id}
            archive={archive}
            onBack={() => setArchive(null)}
            onImported={(imported) => {
              setDone(imported)
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

const NEW = 'new'
const EXISTING = 'existing'

function ImportForm({
  archive,
  onBack,
  onImported,
}: {
  archive: ArchiveOutcome
  onBack: () => void
  onImported: (imported: ImportedPrint) => void
}) {
  const [printer, setPrinter] = useState(false)
  const found = useAsync(() => api.getPrintSources(archive.id, { printer }), [archive.id, printer])
  // The last answer stays while the printer is read, so the list never blinks.
  const [last, setLast] = useState(found.data)
  if (found.data && found.data !== last) setLast(found.data)
  const sources = found.data ?? last
  const models = useAsync(() => api.listModels(), [])

  const [picked, setPicked] = useState<string | null>(null)
  const preselected = sources?.sources.find((s) => s.preselected)
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

  const newBlocked = source?.sliced ?? false
  const ready = source !== null && (mode === EXISTING ? slug !== '' : name.trim() !== '' && !newBlocked)

  async function submit() {
    if (!source) return
    const body: PrintImportRequest = {
      source: { kind: source.kind, library_file_id: source.library_file_id ?? null, path: source.path ?? null },
      ...(mode === EXISTING ? { slug } : { new_model: { name: name.trim(), description: '' } }),
    }
    setBusy(true)
    setError(null)
    try {
      onImported(await api.importPrint(archive.id, body))
    } catch (caught) {
      setError(errorText(caught))
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
        <Button type="button" variant="ghost" size="sm" onClick={onBack} disabled={busy}>
          Choose another print
        </Button>
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
          ) : null}
          .{' '}
          <Link className="underline" to={`/prints/${archive.id}`}>
            Open the print
          </Link>
        </p>
      ) : (
        <>
          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">Where is its 3MF?</legend>
            <RadioGroup aria-label="Source" value={choice ?? ''} onValueChange={setPicked} className="gap-1">
              {sources.sources.map((s) => {
                const key = sourceKey(s)
                const facts = [
                  s.name ?? '',
                  typeof s.size === 'number' ? formatBytes(s.size) : '',
                  s.modified_at ? timeAgo(s.modified_at) : '',
                  s.sliced ? 'sliced' : '',
                  s.matched_by === 'hash' ? 'same file' : s.matched_by === 'name' ? 'same name' : '',
                ].filter(Boolean)
                return (
                  <label key={key} className={`flex items-start gap-2 text-sm ${s.available ? '' : 'opacity-60'}`} data-testid={`source-${key}`}>
                    <RadioGroupItem value={key} disabled={!s.available} aria-label={`${KIND_LABEL[s.kind]} ${s.name ?? ''}`.trim()} className="mt-0.5" />
                    <span className="flex min-w-0 flex-col">
                      <span>{KIND_LABEL[s.kind]}</span>
                      {facts.length > 0 && <span className="break-all text-xs text-muted-foreground">{facts.join(' · ')}</span>}
                      {!s.available && s.reason && <span className="text-xs text-warn">{s.reason}</span>}
                    </span>
                  </label>
                )
              })}
            </RadioGroup>
            {!printer && (
              <div className="flex flex-col items-start gap-1">
                <Button type="button" variant="outline" size="sm" onClick={() => setPrinter(true)} disabled={busy}>
                  Also look on the printer
                </Button>
                <span className="text-xs text-muted-foreground">
                  Lists the printer's storage. Its file is copied into Bambuddy's library if you import from it.
                </span>
              </div>
            )}
            {printer && found.loading && <p className="text-xs text-muted-foreground">Asking the printer…</p>}
            {printer && found.error && <p className="text-xs text-warn">Could not ask the printer: {errorText(found.error)}</p>}
          </fieldset>

          <fieldset className="flex flex-col gap-2">
            <legend className="mb-1 text-sm font-medium">Which model is it a print of?</legend>
            <RadioGroup aria-label="Model" value={mode} onValueChange={setMode} className="gap-2">
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
                      A sliced file has no meshes to open as a model. Choose the slicer project or a library 3MF.
                    </span>
                  )}
                </div>
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
