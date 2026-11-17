import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { listEvents, listSpools, type EventQuery, type LedgerRow } from '../../api/filament'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { TD, TH, when } from '../../components/filament/formClasses'
import { grams, spoolPath } from '../../components/filament/spoolsQuery'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'

type Kind = NonNullable<EventQuery['kind']>
type Source = NonNullable<EventQuery['source']>

const ANY = '__any'
const KINDS: { id: Kind; title: string }[] = [
  { id: 'created', title: 'Created' },
  { id: 'loaded', title: 'Loaded' },
  { id: 'unloaded', title: 'Unloaded' },
  { id: 'moved', title: 'Moved' },
  { id: 'print_started', title: 'Print started' },
  { id: 'print_ended', title: 'Print ended' },
  { id: 'weighed', title: 'Weighed' },
  { id: 'ams_reading', title: 'AMS reading' },
  { id: 'dried', title: 'Dried' },
  { id: 'edited', title: 'Edited' },
  { id: 'tag_bound', title: 'Tag bound' },
  { id: 'tag_unbound', title: 'Tag taken off' },
  { id: 'archived', title: 'Archived' },
  { id: 'restored', title: 'Restored' },
  { id: 'synced_from_bb', title: 'Synced from Bambuddy' },
]
const SOURCES: { id: Source; title: string }[] = [
  { id: 'person', title: 'Person' },
  { id: 'scale', title: 'Scale' },
  { id: 'ams_remain', title: 'AMS remaining' },
  { id: 'print_estimate', title: 'Print estimate' },
  { id: 'print_actual', title: 'Print actual' },
  { id: 'bb', title: 'Bambuddy' },
]

const PAGE = 50
const titleOf = (list: { id: string; title: string }[], id: string | null) => list.find((one) => one.id === id)?.title ?? id ?? ''

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The ledger could not be loaded. Try again.')

/** The first instant after the local day `day` (`YYYY-MM-DD`), as an ISO time; `null` for a bad day. */
function dayAfter(day: string | null): string | null {
  if (!day) return null
  const at = new Date(`${day}T00:00:00`)
  if (Number.isNaN(at.getTime())) return null
  at.setDate(at.getDate() + 1)
  return at.toISOString()
}

/** A row's grams change, as "268 g to 120 g (-148 g)"; empty when it has none. */
function change(row: LedgerRow): string {
  const parts: string[] = []
  if (row.grams_before != null || row.grams_after != null) parts.push(`${grams(row.grams_before) || '?'} g to ${grams(row.grams_after) || '?'} g`)
  if (row.delta_g != null) parts.push(`(${row.delta_g > 0 ? '+' : ''}${grams(row.delta_g)} g)`)
  return parts.join(' ')
}

/** What else the row says: the run it belongs to, a drying's settings, a tag's reason. */
function detail(row: LedgerRow): string {
  const parts: string[] = []
  if (row.ref_kind) parts.push(`${row.ref_kind} ${row.ref_id ?? ''}`.trim())
  const { temp_c, hours, reason } = row.data as { temp_c?: unknown; hours?: unknown; reason?: unknown }
  if (temp_c != null) parts.push(`${String(temp_c)} °C`)
  if (hours != null) parts.push(`${String(hours)} h`)
  if (typeof reason === 'string') parts.push(reason)
  return parts.join(', ')
}

/**
 * #6262 — the Ledger tab: every spool's events, newest first, in pages. "Older" passes the
 * last page's `next_cursor` back as `cursor` (F4), so no row is lost or repeated. The
 * filters (spool number, kind, source, a from and a to day) are in the URL; From is sent as
 * `since` (the day's start) and To as `until` (the start of the day after it).
 */
export function LedgerTab() {
  const [params, setParams] = useSearchParams()
  const spoolNumber = params.get('spool') ?? ''
  const kind = KINDS.find((one) => one.id === params.get('kind'))?.id ?? ''
  const source = SOURCES.find((one) => one.id === params.get('source'))?.id ?? ''
  const from = params.get('from') ?? ''
  const to = params.get('to') ?? ''

  const [rows, setRows] = useState<LedgerRow[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [numbers, setNumbers] = useState<Map<number, string> | null>(null)
  const latest = useRef(0)

  useEffect(() => {
    let live = true
    listSpools({ includeArchived: true }).then(
      (spools) => live && setNumbers(new Map(spools.map((spool) => [spool.number, spool.id]))),
      (cause) => live && setError(message(cause)),
    )
    return () => {
      live = false
    }
  }, [])

  // The spool filter is a number; an unknown one asks for nothing.
  const spoolId = useMemo(() => (spoolNumber && numbers ? (numbers.get(Number(spoolNumber)) ?? null) : undefined), [spoolNumber, numbers])
  const unknownSpool = spoolNumber !== '' && numbers !== null && spoolId === null
  const waiting = spoolNumber !== '' && numbers === null

  const fetchPage = useCallback(
    async (start: string | null) => {
      const query: EventQuery = { limit: PAGE, cursor: start }
      if (spoolId) query.spool_id = spoolId
      if (kind) query.kind = kind
      if (source) query.source = source
      const since = from ? new Date(`${from}T00:00:00`) : null
      if (since && !Number.isNaN(since.getTime())) query.since = since.toISOString()
      const until = dayAfter(to)
      if (until) query.until = until
      const page = await listEvents({ ...query, cursor: start })
      return { events: page.events, next: page.next_cursor ?? null }
    },
    [spoolId, kind, source, from, to],
  )

  useEffect(() => {
    if (waiting) return
    const mine = ++latest.current
    if (unknownSpool) {
      setRows([])
      setCursor(null)
      setError(null)
      return
    }
    setRows(null)
    fetchPage(null).then(
      (page) => {
        if (mine !== latest.current) return
        setRows(page.events)
        setCursor(page.next)
        setError(null)
      },
      (cause) => {
        if (mine === latest.current) setError(message(cause))
      },
    )
  }, [fetchPage, waiting, unknownSpool])

  async function older() {
    if (!cursor) return
    const mine = latest.current
    setBusy(true)
    try {
      const page = await fetchPage(cursor)
      if (mine !== latest.current) return
      setRows((now) => [...(now ?? []), ...page.events])
      setCursor(page.next)
      setError(null)
    } catch (cause) {
      setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  const set = (name: string, value: string) => {
    const next = new URLSearchParams(params)
    if (value) next.set(name, value)
    else next.delete(name)
    setParams(next, { replace: true })
  }
  const filtered = params.size > 0
  const numberOf = (row: LedgerRow) => `#${row.spool_number}`

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end gap-3 text-[13px]">
        <label className="flex flex-col gap-1 text-muted-foreground">
          Spool number
          <Input
            type="number"
            min={1}
            inputMode="numeric"
            className="w-28 text-[13px]"
            value={spoolNumber}
            onChange={(event) => set('spool', event.target.value)}
          />
        </label>
        <label className="flex flex-col gap-1 text-muted-foreground">
          Kind
          <Select value={kind || ANY} onValueChange={(next) => set('kind', next === ANY ? '' : next)}>
            <SelectTrigger aria-label="Kind" className="h-8 w-auto text-[13px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>Every kind</SelectItem>
              {KINDS.map((one) => (
                <SelectItem key={one.id} value={one.id}>
                  {one.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-muted-foreground">
          Source
          <Select value={source || ANY} onValueChange={(next) => set('source', next === ANY ? '' : next)}>
            <SelectTrigger aria-label="Source" className="h-8 w-auto text-[13px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>Every source</SelectItem>
              {SOURCES.map((one) => (
                <SelectItem key={one.id} value={one.id}>
                  {one.title}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="flex flex-col gap-1 text-muted-foreground">
          From
          <Input type="date" className="w-40 text-[13px]" value={from} onChange={(event) => set('from', event.target.value)} />
        </label>
        <label className="flex flex-col gap-1 text-muted-foreground">
          To
          <Input type="date" className="w-40 text-[13px]" value={to} onChange={(event) => set('to', event.target.value)} />
        </label>
        {filtered && (
          <Button type="button" size="sm" variant="ghost" onClick={() => setParams(new URLSearchParams(), { replace: true })}>
            Clear filters
          </Button>
        )}
      </div>
      {unknownSpool && <p className="text-[13px] text-muted-foreground">No spool has the number {spoolNumber}.</p>}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!rows && !error && <p className="text-[13px] text-muted-foreground">Loading the ledger…</p>}
      {rows && (
        <div className="overflow-x-auto rounded-md border">
          <table aria-label="Ledger" className="w-full text-left text-[13px]">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th scope="col" className={TH}>When</th>
                <th scope="col" className={TH}>Spool</th>
                <th scope="col" className={TH}>Event</th>
                <th scope="col" className={TH}>Source</th>
                <th scope="col" className={TH}>Change</th>
                <th scope="col" className={TH}>Detail</th>
                <th scope="col" className={TH}>By</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id} data-event={row.id} className="border-t">
                  <td className={`${TD} whitespace-nowrap`}>
                    <time dateTime={row.at}>{when(row.at)}</time>
                  </td>
                  <td className={TD}>
                    <Link to={spoolPath({ id: row.spool_id })} className="font-medium underline-offset-4 hover:underline">
                      {numberOf(row)}
                    </Link>
                  </td>
                  <td className={TD}>{titleOf(KINDS, row.kind)}</td>
                  <td className={TD}>{titleOf(SOURCES, row.source)}</td>
                  <td className={`${TD} tabular-nums`}>{change(row)}</td>
                  <td className={TD}>{detail(row)}</td>
                  <td className={`${TD} text-muted-foreground`}>{row.actor ?? ''}</td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr className="border-t">
                  <td colSpan={7} className="px-2 py-3 text-muted-foreground">
                    {filtered ? 'No events match.' : 'The ledger is empty. Weigh, move or print a spool to write the first event.'}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {rows && cursor && (
        <div>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={() => void older()}>
            Older
          </Button>
        </div>
      )}
    </div>
  )
}
