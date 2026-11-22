import { useState } from 'react'
import { BoxIcon, HardDriveIcon, LibraryIcon, PrinterIcon } from 'lucide-react'
import { api } from '../api/client'
import type { ArchiveOutcome, LibraryFileSuggestion, LibrarySearchHit, MeshModelCreate, ModelSummary, Output } from '../api/types'
import { timeAgo } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import { useDebounced } from '../lib/useDebounced'
import { LibraryFileChoice } from './library/LibraryFileChoice'
import { PastPrintsPicker } from './orders/PastPrintsPicker'
import { PrintSourceChoice, errorText, sourceChoice, sourceKey, usableSource, usePrintSources } from './prints/PrintSourceChoice'
import { FileDropZone } from './workspace/FileUploadDialog'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

type From = 'disk' | 'library' | 'print' | 'output'

const FROM: { value: From; label: string; icon: typeof BoxIcon }[] = [
  { value: 'disk', label: 'This computer', icon: HardDriveIcon },
  { value: 'library', label: 'Library', icon: LibraryIcon },
  { value: 'print', label: 'A print', icon: PrinterIcon },
  { value: 'output', label: 'An output', icon: BoxIcon },
]

/** How many outputs a search shows. */
const OUTPUTS_SHOWN = 8
const MAX_NAME = 200

/** A file's name without its mesh or slice extension: the model's name it suggests. */
function stem(filename: string): string {
  return filename.replace(/(\.gcode)?\.3mf$|\.stl$/i, '')
}

/** A library hit that holds a mesh: a 3MF or an STL that is not sliced. */
function holdsMesh(hit: LibrarySearchHit): boolean {
  return !hit.sliced && /^(3mf|stl)$/i.test(hit.file_type)
}

/** What the chosen source makes the request from: the body's source fields. */
type Picked = Omit<MeshModelCreate, 'name' | 'description'>

/**
 * #6916 — "Open a mesh as a model": a 3MF or STL becomes an editable mesh model (#2146).
 * The mesh comes from every place Import print reads one: this computer or a session's
 * workspace (#4703, uploaded), a library file found by name, a print (its archive's file,
 * its slicer project, a library match or the printer's storage, through the one picker
 * Import print uses), or an output. A sliced file has no mesh, so it is listed and refused.
 */
export function MeshSourceDialog({
  open,
  onClose,
  onOpened,
}: {
  open: boolean
  onClose: () => void
  onOpened: (model: ModelSummary) => void
}) {
  const [from, setFrom] = useState<From>('disk')
  const [picked, setPicked] = useState<Picked | null>(null)
  const [name, setName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Each open starts on this computer, with nothing chosen.
  const [wasOpen, setWasOpen] = useState(open)
  if (open !== wasOpen) {
    setWasOpen(open)
    if (open) {
      setFrom('disk')
      setPicked(null)
      setName('')
      setError(null)
    }
  }

  function close() {
    if (busy) return
    onClose()
  }

  function choose(next: Picked | null, suggested: string) {
    setPicked(next)
    setName(next ? suggested.slice(0, MAX_NAME) : '')
    setError(null)
  }

  async function submit() {
    if (!picked || name.trim() === '') return
    setBusy(true)
    setError(null)
    try {
      onOpened(await api.createMeshModel({ name: name.trim(), description: '', ...picked }))
      setBusy(false)
      onClose()
    } catch (caught) {
      setBusy(false)
      setError(errorText(caught))
    }
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Open a mesh as a model</DialogTitle>
          <DialogDescription>A 3MF or STL becomes an editable mesh model. A sliced file has no mesh, so it cannot be opened.</DialogDescription>
        </DialogHeader>
        <ToggleGroup
          type="single"
          variant="outline"
          spacing={0}
          aria-label="Where is the mesh?"
          value={from}
          disabled={busy}
          onValueChange={(next) => {
            if (!next) return
            setFrom(next as From)
            choose(null, '')
          }}
          className="w-full flex-wrap"
        >
          {FROM.map(({ value, label, icon: Icon }) => (
            <ToggleGroupItem key={value} value={value} className="flex-1 gap-1.5">
              <Icon aria-hidden />
              {label}
            </ToggleGroupItem>
          ))}
        </ToggleGroup>

        <div className="min-w-0">
          {from === 'disk' ? (
            <FileDropZone
              accept=".3mf,.stl"
              hint="3MF or STL, named after the file"
              workspace={{ resource: null, label: 'a model' }}
              onBusy={setBusy}
              onError={setError}
              onFile={async (file) => {
                onOpened(await api.createMeshModelFromUpload(file))
                onClose()
              }}
            />
          ) : from === 'library' ? (
            <LibraryFileChoice
              id="mesh-library-search"
              chosen={picked?.library_file_id != null ? libraryPick(picked.library_file_id, name) : null}
              onChoose={(file: LibraryFileSuggestion) => choose({ library_file_id: file.id }, stem(file.filename))}
              keep={holdsMesh}
              emptyHint="Search for a 3MF or an STL. A sliced file is left out."
            />
          ) : from === 'print' ? (
            <PrintChoice disabled={busy} onChoose={choose} />
          ) : (
            <OutputChoice open={open} chosen={picked?.output_id ?? null} onChoose={(output) => choose({ output_id: output.id }, output.name || 'Output')} />
          )}
        </div>

        {from !== 'disk' && picked && (
          <div className="flex flex-col gap-1">
            <Label htmlFor="mesh-model-name">Name</Label>
            <Input id="mesh-model-name" value={name} maxLength={MAX_NAME} disabled={busy} onChange={(e) => setName(e.target.value)} />
          </div>
        )}
        {error && (
          <p role="alert" className="text-xs text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={busy}>
            Cancel
          </Button>
          {from !== 'disk' && (
            <Button onClick={() => void submit()} disabled={busy || !picked || name.trim() === ''}>
              {busy ? 'Opening…' : 'Open as a model'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** The chosen library file as its button names it (only its id is compared). */
function libraryPick(id: number, name: string): LibraryFileSuggestion {
  return { id, filename: name, folder_id: null }
}

/**
 * A print, then where its 3MF is: the picker Import print uses, with a sliced file refused.
 * Nothing is chosen for the person: the archive's own file is often the sliced one.
 */
function PrintChoice({ disabled, onChoose }: { disabled: boolean; onChoose: (next: Picked | null, suggested: string) => void }) {
  const [archive, setArchive] = useState<ArchiveOutcome | null>(null)
  if (archive === null) {
    return (
      <PastPrintsPicker
        open
        picked={null}
        disabled={disabled}
        onPick={(next) => {
          setArchive(next)
          onChoose(null, '')
        }}
      />
    )
  }
  return (
    <PrintSources
      key={archive.id}
      archive={archive}
      disabled={disabled}
      onBack={() => {
        setArchive(null)
        onChoose(null, '')
      }}
      onChoose={onChoose}
    />
  )
}

function PrintSources({
  archive,
  disabled,
  onBack,
  onChoose,
}: {
  archive: ArchiveOutcome
  disabled: boolean
  onBack: () => void
  onChoose: (next: Picked | null, suggested: string) => void
}) {
  const read = usePrintSources(archive.id)
  const { found, sources } = read
  const [choice, setChoice] = useState<string | null>(null)
  const title = archive.print_name || `Archive ${archive.id}`
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-sm font-medium">{title}</p>
        <Button type="button" variant="ghost" size="sm" onClick={onBack} disabled={disabled}>
          Choose another print
        </Button>
      </div>
      {found.error && !sources ? (
        <p className="text-xs text-warn">Could not read where the print's file is: {errorText(found.error)}</p>
      ) : !sources ? (
        <p className="text-[13px] text-muted-foreground">Looking for the print's file…</p>
      ) : (
        <PrintSourceChoice
          state={read}
          sources={sources}
          choice={choice}
          disabled={disabled}
          meshOnly
          onChoose={(key) => {
            const source = sources.sources.find((s) => sourceKey(s) === key && usableSource(s, true))
            if (!source) return
            setChoice(key)
            onChoose({ archive_id: archive.id, archive_source: sourceChoice(source) }, stem(source.name ?? archive.print_name ?? ''))
          }}
        />
      )}
    </div>
  )
}

/** One output, newest first, found by its name or its parameters (`GET /outputs`). */
function OutputChoice({ open, chosen, onChoose }: { open: boolean; chosen: string | null; onChoose: (output: Output) => void }) {
  const [query, setQuery] = useState('')
  const q = useDebounced(query.trim(), 250)
  const listed = useAsync(
    () => (open ? api.listRecentOutputs(q ? { q } : {}, { limit: OUTPUTS_SHOWN }) : Promise.resolve({ items: [] as Output[], total: 0 })),
    [open, q],
  )
  const outputs = listed.data?.items ?? []
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Label htmlFor="mesh-output-search">Find an output by name</Label>
      <Input id="mesh-output-search" type="search" value={query} maxLength={200} placeholder="Newest first" onChange={(e) => setQuery(e.target.value)} />
      {listed.error ? (
        <span className="text-xs text-warn">Could not list the outputs: {errorText(listed.error)}</span>
      ) : !listed.data ? (
        <span className="text-xs text-muted-foreground">Listing the outputs…</span>
      ) : outputs.length === 0 ? (
        <span className="text-xs text-muted-foreground">{q ? 'No output has that name.' : 'No output is saved yet.'}</span>
      ) : (
        <ul className="flex flex-col divide-y rounded-md border" aria-label="Outputs">
          {outputs.map((output) => (
            <li key={output.id}>
              <button
                type="button"
                aria-pressed={output.id === chosen}
                onClick={() => onChoose(output)}
                className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-sm hover:bg-muted/60 focus-visible:outline-2 focus-visible:outline-ring aria-pressed:bg-secondary"
              >
                {output.has_thumbnail ? (
                  <img src={api.outputThumbnailUrl(output.id, output.thumbnail_version)} alt="" className="size-9 shrink-0 rounded border object-contain" loading="lazy" />
                ) : (
                  <span aria-hidden className="size-9 shrink-0 rounded border bg-muted" />
                )}
                <span className="flex min-w-0 flex-col">
                  <span className="truncate">{output.name || 'Untitled output'}</span>
                  <span className="truncate text-xs text-muted-foreground">
                    {output.slug}, saved {timeAgo(output.created_at)}
                  </span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
