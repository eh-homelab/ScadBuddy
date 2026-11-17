import { Loader2Icon } from 'lucide-react'
import { useId, useRef, useState } from 'react'
import { ApiError } from '../../api/client'
import { importSpools, type CsvRow, type ImportPreview, type ImportRowResult } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'
import { readSpoolsCsv } from './spoolsCsv'

interface Props {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** After a write: the tab reloads its spools. */
  onImported: (done: ImportPreview) => void
}

interface Opened {
  name: string
  rows: CsvRow[]
  ignored: string[]
  unknown: string[]
  preview: ImportPreview | null
  /** One `Idempotency-Key` per opened preview: a retried Import writes once. */
  key: string
}

const ACTION: Record<ImportRowResult['action'], string> = { create: 'New', update: 'Change', error: 'Error' }

function shown(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'none'
  return Array.isArray(value) ? value.join(' ') : String(value)
}

/** A row's changes as "field: before → after", a new catalog name marked so. */
function Changes({ changes }: { changes: ImportRowResult['changes'] }) {
  const entries = Object.entries(changes)
  if (!entries.length) return <span className="text-muted-foreground">No change</span>
  return (
    <ul className="space-y-0.5">
      {entries.map(([field, [before, after, mark]]) => (
        <li key={field}>
          <span className="text-muted-foreground">{field}:</span> {before == null ? '' : `${shown(before)} → `}
          <span className="font-medium">{shown(after)}</span>
          {mark && typeof mark === 'object' && 'new' in mark ? <span className="text-muted-foreground"> (new to the catalog)</span> : null}
        </li>
      ))}
    </ul>
  )
}

/**
 * #6262 — the spools CSV import (plan F24): choose a file, see what each line would do
 * (a dry run), then import it. The browser reads the file and sends its cells as JSON.
 * Import is on only when no line is an error; one key per opened preview, so a retry
 * after a lost answer gets the first result and writes nothing.
 */
export function SpoolImport({ open, onOpenChange, onImported }: Props) {
  const fileId = useId()
  const input = useRef<HTMLInputElement>(null)
  const [opened, setOpened] = useState<Opened | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [working, setWorking] = useState<'preview' | 'write' | null>(null)

  /** Bumped by each chosen file and by close: a dry run that answers after either is dropped. */
  const latest = useRef(0)

  const close = (next: boolean) => {
    if (next) return onOpenChange(true)
    // The write commits whatever the dialog does, so it stays open until the answer.
    if (working === 'write') return
    latest.current += 1
    setBusy(false)
    setWorking(null)
    setOpened(null)
    setError(null)
    onOpenChange(false)
  }

  const choose = async (file: File | undefined) => {
    if (!file) return
    const mine = ++latest.current
    setError(null)
    const read = readSpoolsCsv(await file.text())
    if (mine !== latest.current) return
    const next: Opened = { name: file.name, ...read, preview: null, key: crypto.randomUUID() }
    setOpened(next)
    if (!read.rows.length || read.unknown.length) return
    setBusy(true)
    setWorking('preview')
    try {
      const preview = await importSpools(read.rows, { dryRun: true })
      if (mine === latest.current) setOpened({ ...next, preview })
    } catch (cause) {
      if (mine === latest.current) setError(cause instanceof ApiError ? cause.detail : 'The preview failed. Try again.')
    } finally {
      if (mine === latest.current) {
        setBusy(false)
        setWorking(null)
      }
    }
  }

  const write = async () => {
    if (!opened) return
    setBusy(true)
    setWorking('write')
    setError(null)
    try {
      const done = await importSpools(opened.rows, { dryRun: false, key: opened.key })
      onImported(done)
      close(false)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'The import failed. Try again.')
    } finally {
      setBusy(false)
      setWorking(null)
    }
  }

  const rows = opened?.preview?.rows ?? []
  const errors = rows.filter((row) => row.action === 'error').length
  const ready = Boolean(opened?.preview) && errors === 0 && rows.length > 0 && !busy

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Import spools</DialogTitle>
          <DialogDescription>
            A CSV with the export's columns. A line with a spool number changes that spool where a cell differs; a line
            without one adds a spool. Empty cells change nothing.
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-wrap items-center gap-2 text-sm">
          <label htmlFor={fileId} className="sr-only">
            CSV file
          </label>
          <input
            ref={input}
            id={fileId}
            type="file"
            accept=".csv,text/csv"
            className="sr-only"
            onChange={(event) => {
              const file = event.target.files?.[0]
              // Cleared at once, so choosing the same path again fires a change.
              event.target.value = ''
              void choose(file)
            }}
          />
          <Button type="button" variant="outline" size="sm" onClick={() => input.current?.click()}>
            {opened ? 'Choose another file' : 'Choose a file'}
          </Button>
          {opened && <span className="text-muted-foreground">{opened.name}</span>}
        </div>

        <div role="status" aria-live="polite" className="empty:hidden">
          {working && (
            <p className="flex items-center gap-2 text-[13px] text-muted-foreground">
              <Loader2Icon className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden />
              {working === 'preview' ? 'Checking each line of the file' : 'Importing the spools'}
            </p>
          )}
        </div>

        {opened && opened.ignored.length > 0 && (
          <p className="text-[13px] text-muted-foreground">
            Ignored: {opened.ignored.join(', ')}. These are worked out from each spool's ledger, so an import does not set
            them.
          </p>
        )}
        {opened && opened.unknown.length > 0 && (
          <p role="alert" className="text-[13px] text-destructive">
            Unknown columns: {opened.unknown.join(', ')}. Rename or remove them, then choose the file again.
          </p>
        )}
        {opened && !opened.rows.length && <p className="text-[13px] text-muted-foreground">The file has no spool lines.</p>}

        {rows.length > 0 && (
          <div className="overflow-x-auto rounded-md border">
            <table aria-label="Import preview" className="w-full text-left text-[13px]">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th className="px-2 py-1.5 font-medium">Line</th>
                  <th className="px-2 py-1.5 font-medium">Action</th>
                  <th className="px-2 py-1.5 text-right font-medium">Spool</th>
                  <th className="px-2 py-1.5 font-medium">What changes</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.line} aria-label={`Line ${row.line}`} className="border-t align-top">
                    <td className="px-2 py-1.5 text-right tabular-nums">{row.line}</td>
                    <td className={cn('px-2 py-1.5', row.action === 'error' && 'font-medium text-destructive')}>{ACTION[row.action]}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums">{row.number == null ? '' : `#${row.number}`}</td>
                    <td className="px-2 py-1.5">
                      {row.action === 'error' ? <span className="text-destructive">{row.error}</span> : <Changes changes={row.changes} />}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {errors > 0 && (
          <p className="text-[13px] text-destructive">
            {errors === 1 ? 'One line has an error' : `${errors} lines have errors`}. Fix the file and choose it again; nothing is
            imported while any line is wrong.
          </p>
        )}
        {error && (
          <p role="alert" className="text-[13px] text-destructive">
            {error}
          </p>
        )}

        <DialogFooter>
          <Button type="button" variant="ghost" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button type="button" disabled={!ready} onClick={() => void write()}>
            {rows.length ? `Import ${rows.length} ${rows.length === 1 ? 'spool' : 'spools'}` : 'Import'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
