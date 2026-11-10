import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { ArchiveOutcome, FarmArchivesQuery } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const ANY = 'any'
/** How many archives a page lists; "Load more" adds as many again, up to the route's 200. */
const PAGE = 50
const MOST = 200
/** The statuses Bambuddy gives an archive that the filter offers. */
const STATUSES = [
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'cancelled', label: 'Cancelled' },
]

function minutes(seconds: number | null | undefined): string {
  if (!seconds) return ''
  const total = Math.round(seconds / 60)
  return total >= 60 ? `${Math.floor(total / 60)} h ${total % 60} min` : `${total} min`
}

/**
 * The Add item dialog's "Past prints" (#4777, order operations spec 2026-11-07 §3.2):
 * every Bambuddy archive, ScadBuddy's or not (`GET /farm/archives`), newest first,
 * filtered by day, printer and project (the route's filters) and by status and name (over
 * the archives listed). One is picked; the backend makes it an item of the output or
 * library file that printed it, or of a copy of its file in the native library.
 */
export function PastPrintsPicker({
  open,
  picked,
  onPick,
  disabled,
  unlinkedOnly = false,
}: {
  open: boolean
  picked: ArchiveOutcome | null
  onPick: (next: ArchiveOutcome | null) => void
  disabled?: boolean
  /** #4698 — lists only the archives no print link names (`scadbuddy_print` not true). */
  unlinkedOnly?: boolean
}) {
  const [fromDay, setFromDay] = useState('')
  const [toDay, setToDay] = useState('')
  const [printer, setPrinter] = useState(ANY)
  const [project, setProject] = useState(ANY)
  const [state, setState] = useState(ANY)
  const [text, setText] = useState('')
  const [limit, setLimit] = useState(PAGE)

  const printers = useAsync(() => (open ? api.listPrinters() : Promise.resolve([])), [open])
  const projects = useAsync(() => (open ? api.getProjects() : Promise.resolve(null)), [open])
  const printerChoices = useMemo(
    () => (printers.data ?? []).flatMap((p) => (typeof p.bambuddy_printer_id === 'number' ? [{ id: p.bambuddy_printer_id, name: p.name }] : [])),
    [printers.data],
  )

  const filters = useMemo<FarmArchivesQuery>(
    () => ({
      ...(fromDay ? { date_from: fromDay } : {}),
      ...(toDay ? { date_to: toDay } : {}),
      ...(printer !== ANY ? { printer_id: Number(printer) } : {}),
      ...(project !== ANY ? { project_id: Number(project) } : {}),
    }),
    [fromDay, toDay, printer, project],
  )
  useEffect(() => setLimit(PAGE), [filters])

  const listed = useAsync(() => (open ? api.listFarmArchives(filters, { limit }) : Promise.resolve([] as ArchiveOutcome[])), [open, filters, limit])
  // The last answer stays on screen while another loads, so the list never blinks.
  const [lastPage, setLastPage] = useState(listed.data)
  if (listed.data && listed.data !== lastPage) setLastPage(listed.data)
  const page = listed.data ?? (listed.loading ? lastPage : undefined)
  // #5992: with `unlinkedOnly`, a full page of ScadBuddy's own prints lists nothing to
  // pick, so the next page is read until one passes, or the route's most.
  const allLinked = unlinkedOnly && !!listed.data && listed.data.length === limit && listed.data.every((a) => a.scadbuddy_print === true)
  useEffect(() => {
    if (allLinked && limit < MOST) setLimit((n) => Math.min(n + PAGE, MOST))
  }, [allLinked, limit])
  const rows = useMemo(() => {
    const q = text.trim().toLocaleLowerCase()
    return (page ?? []).filter(
      (a) =>
        (!unlinkedOnly || a.scadbuddy_print !== true) &&
        (state === ANY || a.status === state) &&
        (!q || (a.print_name ?? '').toLocaleLowerCase().includes(q)),
    )
  }, [page, state, text, unlinkedOnly])
  const printerName = (id: number | null | undefined) => printerChoices.find((p) => p.id === id)?.name ?? (id ? `Printer ${id}` : 'No printer')

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap gap-3">
        <div className="flex flex-col gap-1">
          <Label htmlFor="past-from">From</Label>
          <Input id="past-from" type="date" value={fromDay} onChange={(e) => setFromDay(e.target.value)} />
        </div>
        <div className="flex flex-col gap-1">
          <Label htmlFor="past-to">To</Label>
          <Input id="past-to" type="date" value={toDay} onChange={(e) => setToDay(e.target.value)} />
        </div>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Select value={printer} onValueChange={setPrinter}>
          <SelectTrigger aria-label="Printer">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Any printer</SelectItem>
            {printerChoices.map((p) => (
              <SelectItem key={p.id} value={String(p.id)}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={project} onValueChange={setProject}>
          <SelectTrigger aria-label="Project">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Any project</SelectItem>
            {(projects.data?.projects ?? []).map((p) => (
              <SelectItem key={p.id} value={String(p.id)}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={state} onValueChange={setState}>
          <SelectTrigger aria-label="Status">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Any status</SelectItem>
            {STATUSES.map((s) => (
              <SelectItem key={s.value} value={s.value}>
                {s.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input aria-label="Search past prints" type="search" placeholder="Name" maxLength={256} value={text} onChange={(e) => setText(e.target.value)} />
      </div>
      {listed.error ? (
        <p className="text-xs text-warn">Could not list the past prints: {listed.error.message}</p>
      ) : !page ? (
        <p className="text-[13px] text-muted-foreground">Loading past prints…</p>
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No past print matches{page.length > 0 ? ' in the prints listed' : ''}.</p>
      ) : (
        <ul aria-label="Past prints" className="flex max-h-72 flex-col overflow-y-auto rounded-md border">
          {rows.map((archive) => {
            const name = archive.print_name || `Archive ${archive.id}`
            const on = picked?.id === archive.id
            const when = archive.completed_at ?? archive.created_at
            return (
              <li key={archive.id} className="border-b last:border-b-0">
                <button
                  type="button"
                  aria-pressed={on}
                  disabled={disabled}
                  data-testid={`past-${archive.id}`}
                  className={`flex w-full flex-col items-start px-3 py-1.5 text-left text-sm hover:bg-muted focus-visible:bg-muted focus-visible:outline-none ${on ? 'bg-muted' : ''}`}
                  onClick={() => onPick(on ? null : archive)}
                >
                  <span className="font-medium">{name}</span>
                  <span className="text-xs text-muted-foreground">
                    {[
                      archive.status ?? 'unknown status',
                      printerName(archive.printer_id),
                      archive.plate_id ? `plate ${archive.plate_id}` : '',
                      minutes(archive.actual_time_seconds ?? archive.print_time_seconds),
                      archive.filament_used_grams ? `${archive.filament_used_grams.toFixed(1)} g` : '',
                      when ? timeAgo(when) : '',
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
      {page && page.length >= limit && limit < MOST && (
        <Button type="button" variant="ghost" size="sm" className="self-start" onClick={() => setLimit((n) => Math.min(n + PAGE, MOST))}>
          Load more
        </Button>
      )}
    </div>
  )
}
