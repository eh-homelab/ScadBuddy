import { useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { AutoImport, ImportedPrint } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { ImportPrintDialog, type ImportStart } from './ImportPrintDialog'
import { Button } from '@/components/ui/button'

/** The route's default page; "Show more" reads the next one (`before`, the last archive read). */
const PAGE = 50

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.detail
  return error instanceof Error ? error.message : String(error)
}

/**
 * #4699 phase 2 — "Unsorted imports": the prints the print watcher took but could not link
 * to a model with confidence (`GET /print-imports/auto?state=inbox`), each with why. "Link"
 * opens Import print (#4698) on that print, with the source the watcher recorded chosen;
 * "Dismiss" takes it out of the inbox, and the watcher never takes it again. While a page is
 * full, "Show more" reads the next one. The list reads
 * ScadBuddy only, never Bambuddy, and shows nothing while the inbox is empty. `version`
 * reads it again (the page's own imports).
 */
export function UnsortedImports({
  version = 0,
  onImported,
}: {
  version?: number
  onImported?: (imported: ImportedPrint) => void
}) {
  const [linked, setLinked] = useState(0)
  const inbox = useAsync(() => api.listAutoImports({ state: 'inbox', limit: PAGE }), [version, linked])
  const [gone, setGone] = useState<ReadonlySet<number>>(new Set())
  const [start, setStart] = useState<ImportStart | null>(null)
  const [open, setOpen] = useState(false)
  // The pages "Show more" read after the first; a new first read starts them over.
  const [pages, setPages] = useState<{ first: AutoImport[] | undefined; rows: AutoImport[]; full: boolean }>({
    first: undefined,
    rows: [],
    full: false,
  })
  if (inbox.data !== pages.first) setPages({ first: inbox.data, rows: [], full: (inbox.data?.length ?? 0) === PAGE })
  const [more, setMore] = useState<{ busy: boolean; error: string | null }>({ busy: false, error: null })
  const read = [...(inbox.data ?? []), ...pages.rows]
  const rows = read.filter((row) => !gone.has(row.archive_id))

  async function showMore() {
    const last = read.at(-1)
    if (!last) return
    const first = inbox.data
    setMore({ busy: true, error: null })
    try {
      const next = await api.listAutoImports({ state: 'inbox', limit: PAGE, before: last.archive_id })
      // A page read for an older first page is dropped.
      setPages((was) => (was.first === first ? { ...was, rows: [...was.rows, ...next], full: next.length === PAGE } : was))
      setMore({ busy: false, error: null })
    } catch (caught) {
      setMore({ busy: false, error: errorText(caught) })
    }
  }

  return (
    <>
      {rows.length > 0 && (
        <section className="mb-5" aria-labelledby="unsorted-imports-title" data-testid="unsorted-imports">
          <h2 id="unsorted-imports-title" className="text-sm font-semibold">
            Unsorted imports
          </h2>
          <p className="mt-0.5 mb-2 text-[13px] text-subtle">
            Prints that ScadBuddy did not start. The print watcher imported them, but could not link them to a model
            with confidence.
          </p>
          <ul aria-label="Unsorted imports" className="flex flex-col gap-2">
            {rows.map((row) => (
              <InboxRow
                key={row.archive_id}
                row={row}
                onLink={() => {
                  setStart({
                    archive: { id: row.archive_id, print_name: row.print_name ?? null },
                    recorded: { source: row.source ?? null, library_file_id: row.library_file_id ?? null },
                  })
                  setOpen(true)
                }}
                onDismissed={() => setGone((was) => new Set([...was, row.archive_id]))}
              />
            ))}
          </ul>
          {pages.full && (
            <div className="mt-2 flex flex-col items-start gap-1">
              <Button type="button" size="sm" variant="outline" onClick={() => void showMore()} disabled={more.busy}>
                {more.busy ? 'Reading…' : 'Show more'}
              </Button>
              {more.error && (
                <span className="text-xs text-warn" role="alert">
                  Could not read more: {more.error}
                </span>
              )}
            </div>
          )}
        </section>
      )}
      <ImportPrintDialog
        open={open}
        onOpenChange={setOpen}
        start={start}
        onImported={(imported) => {
          setLinked((n) => n + 1)
          onImported?.(imported)
        }}
      />
    </>
  )
}

function InboxRow({ row, onLink, onDismissed }: { row: AutoImport; onLink: () => void; onDismissed: () => void }) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const name = row.print_name || row.filename || `Archive ${row.archive_id}`

  async function dismiss() {
    setBusy(true)
    setError(null)
    try {
      await api.dismissAutoImport(row.archive_id)
      onDismissed()
    } catch (caught) {
      setError(errorText(caught))
      setBusy(false)
    }
  }

  return (
    <li
      className="flex flex-col gap-1 rounded-md border border-border px-3 py-2 sm:flex-row sm:items-start sm:justify-between"
      data-testid={`unsorted-${row.archive_id}`}
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-sm font-medium break-words">{name}</span>
        <span className="text-xs text-muted-foreground">
          Archive {row.archive_id}
          {row.seen_at ? ` · imported ${timeAgo(row.seen_at)}` : ''}
        </span>
        {row.reason && <span className="text-xs text-warn">{row.reason}</span>}
        {(row.suggested ?? []).length > 0 && (
          <span className="text-xs text-muted-foreground">Maybe: {(row.suggested ?? []).join(', ')}</span>
        )}
        {error && (
          <span className="text-xs text-warn" role="alert">
            {error}
          </span>
        )}
      </div>
      <div className="flex shrink-0 gap-2">
        <Button type="button" size="sm" variant="outline" onClick={onLink} disabled={busy} aria-label={`Link ${name}`}>
          Link…
        </Button>
        <Button
          type="button"
          size="sm"
          variant="ghost"
          onClick={() => void dismiss()}
          disabled={busy}
          aria-label={`Dismiss ${name}`}
        >
          Dismiss
        </Button>
      </div>
    </li>
  )
}
