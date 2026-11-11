import { useMemo, useState } from 'react'
import { Link } from 'react-router'
import { ApiError, api } from '../../api/client'
import type { AutoImport, ImportedPrint, LanPrintRecord } from '../../api/types'
import { modelPath } from '../../lib/deeplink'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { ImportPrintDialog, type ImportStart } from './ImportPrintDialog'
import { lanPrintsActivityPath } from './prints'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** The route's default page; "Show more" reads the next one (`before`, the last id read). */
const PAGE = 50

function errorText(error: unknown): string {
  if (error instanceof ApiError) return error.detail
  return error instanceof Error ? error.message : String(error)
}

type Inbox<T> = {
  rows: T[]
  full: boolean
  more: { busy: boolean; error: string | null }
  showMore: () => Promise<void>
  /** The print left the inbox (dismissed, or a LAN print linked). */
  drop: (id: number) => void
}

/**
 * One inbox list, paged: the first page is read with `deps`; while a page is full, "Show
 * more" reads the next one (`before`, the last id read). The pages "Show more" read after
 * the first, and the prints that left them, start over with a new first read. `emptied` is
 * bumped when the last shown print leaves: the first page is read again, so the older pages'
 * prints show (review of #6028). `drop` decides it from the state it updates, so prints that
 * leave together still bump it (#6029).
 */
function useInbox<T>(read: (before?: number) => Promise<T[]>, idOf: (row: T) => number, deps: unknown[]): Inbox<T> {
  const [pages, setPages] = useState<{
    first: T[] | undefined
    rows: T[]
    full: boolean
    gone: ReadonlySet<number>
    emptied: number
  }>({ first: undefined, rows: [], full: false, gone: new Set(), emptied: 0 })
  const inbox = useAsync(() => read(), [...deps, pages.emptied])
  if (inbox.data !== pages.first) {
    setPages({
      first: inbox.data,
      rows: [],
      full: (inbox.data?.length ?? 0) === PAGE,
      gone: new Set(),
      emptied: pages.emptied,
    })
  }
  const [more, setMore] = useState<{ busy: boolean; error: string | null }>({ busy: false, error: null })
  const readRows = [...(inbox.data ?? []), ...pages.rows]
  const rows = readRows.filter((row) => !pages.gone.has(idOf(row)))

  async function showMore() {
    const last = readRows.at(-1)
    if (!last) return
    const first = inbox.data
    setMore({ busy: true, error: null })
    try {
      const next = await read(idOf(last))
      // A page read for an older first page is dropped.
      setPages((was) => (was.first === first ? { ...was, rows: [...was.rows, ...next], full: next.length === PAGE } : was))
      setMore({ busy: false, error: null })
    } catch (caught) {
      setMore({ busy: false, error: errorText(caught) })
    }
  }

  function drop(id: number) {
    setPages((was) => {
      const gone = new Set([...was.gone, id])
      const left = [...(was.first ?? []), ...was.rows].some((shown) => !gone.has(idOf(shown)))
      return { ...was, gone, emptied: left ? was.emptied : was.emptied + 1 }
    })
  }

  return { rows, full: pages.full, more, showMore, drop }
}

function ShowMore({ inbox }: { inbox: Pick<Inbox<unknown>, 'full' | 'more' | 'showMore'> }) {
  if (!inbox.full) return null
  return (
    <div className="mt-2 flex flex-col items-start gap-1">
      <Button type="button" size="sm" variant="outline" onClick={() => void inbox.showMore()} disabled={inbox.more.busy}>
        {inbox.more.busy ? 'Reading…' : 'Show more'}
      </Button>
      {inbox.more.error && (
        <span className="text-xs text-warn" role="alert">
          Could not read more: {inbox.more.error}
        </span>
      )}
    </div>
  )
}

/**
 * #4699 phase 2 — "Unsorted imports": the prints the print watcher took but could not link
 * to a model with confidence (`GET /print-imports/auto?state=inbox`), each with why. "Link"
 * opens Import print (#4698) on that print, with the source the watcher recorded chosen;
 * "Dismiss" takes it out of the inbox, and the watcher never takes it again.
 *
 * #6041 — beside them, in a list of their own, the prints it recorded on LAN-only printers
 * (`GET /print-imports/auto/lan?state=inbox`), each with its printer's name and its job's
 * name. Such a print has no Bambuddy archive, so it has no Import print: "Link to model…"
 * picks a model only (its one source is the file the watcher kept), and "Dismiss" takes it
 * out of the inbox. Its ids are its own, so it pages apart from the archives.
 *
 * Each list pages on its own (`useInbox`). The lists read ScadBuddy only, never Bambuddy, and
 * show nothing while both inboxes are empty. `version` reads them again (the page's own
 * imports).
 */
export function UnsortedImports({
  version = 0,
  onImported,
}: {
  version?: number
  onImported?: (imported: ImportedPrint) => void
}) {
  const [linked, setLinked] = useState(0)
  const archives = useInbox<AutoImport>(
    (before) => api.listAutoImports({ state: 'inbox', limit: PAGE, ...(before === undefined ? {} : { before }) }),
    (row) => row.archive_id,
    [version, linked],
  )
  const lan = useInbox<LanPrintRecord>(
    (before) => api.listLanAutoImports({ state: 'inbox', limit: PAGE, ...(before === undefined ? {} : { before }) }),
    (row) => row.id ?? 0,
    [version],
  )
  const [start, setStart] = useState<ImportStart | null>(null)
  const [open, setOpen] = useState(false)
  const [linking, setLinking] = useState<LanPrintRecord | null>(null)

  const shown = archives.rows.length > 0 || archives.full || lan.rows.length > 0 || lan.full
  return (
    <>
      {shown && (
        <section className="mb-5" aria-labelledby="unsorted-imports-title" data-testid="unsorted-imports">
          <h2 id="unsorted-imports-title" className="text-sm font-semibold">
            Unsorted imports
          </h2>
          <p className="mt-0.5 mb-2 text-[13px] text-subtle">
            Prints that ScadBuddy did not start. The print watcher imported them, but could not link them to a model
            with confidence.
          </p>
          {(archives.rows.length > 0 || archives.full) && (
            <>
              <ul aria-label="Unsorted imports" className="flex flex-col gap-2">
                {archives.rows.map((row) => (
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
                    onDismissed={() => archives.drop(row.archive_id)}
                  />
                ))}
              </ul>
              <ShowMore inbox={archives} />
            </>
          )}
          {(lan.rows.length > 0 || lan.full) && (
            <>
              <h3 className="mt-3 mb-1 text-xs font-medium text-subtle">On printers with no Bambuddy link</h3>
              <ul aria-label="Unsorted imports on LAN-only printers" className="flex flex-col gap-2">
                {lan.rows.map((row) => (
                  <LanInboxRow
                    key={row.id}
                    row={row}
                    onLink={() => setLinking(row)}
                    onDismissed={() => lan.drop(row.id ?? 0)}
                  />
                ))}
              </ul>
              <ShowMore inbox={lan} />
            </>
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
      <LanLinkDialog
        row={linking}
        onOpenChange={(next) => {
          if (!next) setLinking(null)
        }}
        onLinked={(row) => lan.drop(row.id ?? 0)}
      />
    </>
  )
}

function useDismiss(dismiss: () => Promise<unknown>, onDismissed: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function run() {
    setBusy(true)
    setError(null)
    try {
      await dismiss()
      onDismissed()
    } catch (caught) {
      setError(errorText(caught))
      setBusy(false)
    }
  }
  return { busy, error, run }
}

function RowActions({
  name,
  linkLabel,
  busy,
  onLink,
  onDismiss,
}: {
  name: string
  linkLabel: string
  busy: boolean
  onLink: () => void
  onDismiss: () => void
}) {
  return (
    <div className="flex shrink-0 gap-2">
      <Button type="button" size="sm" variant="outline" onClick={onLink} disabled={busy} aria-label={`Link ${name}`}>
        {linkLabel}
      </Button>
      <Button type="button" size="sm" variant="ghost" onClick={onDismiss} disabled={busy} aria-label={`Dismiss ${name}`}>
        Dismiss
      </Button>
    </div>
  )
}

function RowNotes({ reason, suggested, error }: { reason?: string | null; suggested?: string[] | null; error: string | null }) {
  return (
    <>
      {reason && <span className="text-xs text-warn">{reason}</span>}
      {(suggested ?? []).length > 0 && (
        <span className="text-xs text-muted-foreground">Maybe: {(suggested ?? []).join(', ')}</span>
      )}
      {error && (
        <span className="text-xs text-warn" role="alert">
          {error}
        </span>
      )}
    </>
  )
}

function InboxRow({ row, onLink, onDismissed }: { row: AutoImport; onLink: () => void; onDismissed: () => void }) {
  const name = row.print_name || row.filename || `Archive ${row.archive_id}`
  const dismiss = useDismiss(() => api.dismissAutoImport(row.archive_id), onDismissed)
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
        <RowNotes reason={row.reason} suggested={row.suggested} error={dismiss.error} />
      </div>
      <RowActions name={name} linkLabel="Link…" busy={dismiss.busy} onLink={onLink} onDismiss={() => void dismiss.run()} />
    </li>
  )
}

function LanInboxRow({ row, onLink, onDismissed }: { row: LanPrintRecord; onLink: () => void; onDismissed: () => void }) {
  const name = row.subtask_name
  const dismiss = useDismiss(() => api.dismissLanAutoImport(row.id ?? 0), onDismissed)
  return (
    <li
      className="flex flex-col gap-1 rounded-md border border-border px-3 py-2 sm:flex-row sm:items-start sm:justify-between"
      data-testid={`unsorted-lan-${row.id}`}
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        <span className="text-sm font-medium break-words">{name}</span>
        <span className="text-xs text-muted-foreground">
          {row.printer_name ?? 'A deleted printer'}
          {row.seen_at ? ` · recorded ${timeAgo(row.seen_at)}` : ''}
        </span>
        <RowNotes reason={row.reason} suggested={row.suggested} error={dismiss.error} />
      </div>
      <RowActions
        name={name}
        linkLabel="Link to model…"
        busy={dismiss.busy}
        onLink={onLink}
        onDismiss={() => void dismiss.run()}
      />
    </li>
  )
}

/**
 * #6041 — "Link to model…" for a LAN-only printer's print: a model picker only, the models
 * the job's name suggests first. There is no source step: the print's one source is the file
 * the watcher kept. A linked LAN print is not on the model's Prints tab (that list is
 * Bambuddy's archives); the answer says it is in Activity.
 */
function LanLinkDialog({
  row,
  onOpenChange,
  onLinked,
}: {
  row: LanPrintRecord | null
  onOpenChange: (open: boolean) => void
  onLinked: (row: LanPrintRecord) => void
}) {
  return (
    <Dialog open={row !== null} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Link to model</DialogTitle>
          <DialogDescription>
            Link a print from a printer with no Bambuddy link to a model. Its 3MF is the file the print watcher kept.
          </DialogDescription>
        </DialogHeader>
        {row && <LanLinkForm key={row.id} row={row} onClose={() => onOpenChange(false)} onLinked={onLinked} />}
      </DialogContent>
    </Dialog>
  )
}

function LanLinkForm({
  row,
  onClose,
  onLinked,
}: {
  row: LanPrintRecord
  onClose: () => void
  onLinked: (row: LanPrintRecord) => void
}) {
  const models = useAsync(() => api.listModels(), [])
  const choices = useMemo(() => {
    const all = models.data ?? []
    const named = new Map(all.map((m) => [m.slug, m.name]))
    const suggested = (row.suggested ?? []).filter((slug) => named.has(slug))
    const first = new Set(suggested)
    return {
      suggested: suggested.map((slug) => ({ slug, name: named.get(slug) ?? slug })),
      all: [
        ...suggested.map((slug) => ({ slug, name: named.get(slug) ?? slug })),
        ...all.filter((m) => !first.has(m.slug)).map((m) => ({ slug: m.slug, name: m.name })),
      ],
    }
  }, [models.data, row.suggested])
  const [target, setTarget] = useState<string | null>(null)
  const slug = target ?? choices.suggested[0]?.slug ?? ''
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<LanPrintRecord | null>(null)

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      const linked = await api.linkLanAutoImport(row.id ?? 0, slug)
      setDone(linked)
      onLinked(linked)
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  if (done?.slug) {
    return (
      <>
        <div className="flex flex-col gap-2 text-sm" data-testid="lan-link-done">
          <p>
            {row.subtask_name} is now a print of{' '}
            <Link className="underline" to={modelPath(done.slug)}>
              {done.slug}
            </Link>
            .
          </p>
          <p className="text-[13px] text-subtle">
            A print from a printer with no Bambuddy link is not on the model's Prints tab.{' '}
            <Link className="underline" to={lanPrintsActivityPath(done.slug)}>
              See it in Activity
            </Link>
            .
          </p>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>Close</Button>
        </DialogFooter>
      </>
    )
  }

  return (
    <>
      <div className="flex flex-col gap-2 text-sm">
        <p data-testid="lan-link-print">
          <span className="font-medium">{row.subtask_name}</span>
          <span className="text-muted-foreground"> · {row.printer_name ?? 'a deleted printer'}</span>
        </p>
        {choices.suggested.length > 0 && (
          <div className="flex flex-wrap gap-1" aria-label="Suggested models">
            {choices.suggested.map((m) => (
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
            {choices.all.map((m) => (
              <SelectItem key={m.slug} value={m.slug}>
                {m.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        {error && (
          <span className="text-xs text-warn" role="alert">
            {error}
          </span>
        )}
      </div>
      <DialogFooter>
        <Button variant="outline" onClick={onClose}>
          Cancel
        </Button>
        <Button onClick={() => void submit()} disabled={busy || slug === ''}>
          {busy ? 'Linking…' : 'Link'}
        </Button>
      </DialogFooter>
    </>
  )
}
