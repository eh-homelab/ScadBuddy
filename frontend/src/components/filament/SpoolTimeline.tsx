import { useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import type { SpoolEvent, TimelineRow } from '../../api/filament'
import { Button } from '@/components/ui/button'
import { actorText, changeEntries, eventNames, fromTypeText, KIND_TEXT, valueText, type EventNames } from './eventText'
import { EntityLink } from './EntityLink'
import { when } from './formClasses'

/** The timeline's filters, each the kinds it keeps; "All" keeps every kind. */
const FILTERS = [
  { label: 'All', kinds: null },
  { label: 'Prints', kinds: ['print_started', 'print_ended'] },
  { label: 'Weighings', kinds: ['weighed', 'ams_reading'] },
  { label: 'Edits', kinds: ['edited', 'synced_from_bb', 'archived', 'restored', 'consumed_reset'] },
  { label: 'Moves', kinds: ['moved', 'loaded', 'unloaded'] },
  { label: 'Tags', kinds: ['tag_bound', 'tag_unbound'] },
] as const satisfies readonly { label: string; kinds: readonly SpoolEvent['kind'][] | null }[]

type Filter = (typeof FILTERS)[number]['label']

const SOURCE: Record<string, string> = { scale: 'scale', person: 'by hand', ams_remain: 'AMS', bb: 'Bambuddy', print_actual: 'measured', print_estimate: 'estimate' }

const grams = (value: number) => `${Number(value.toFixed(1))} g`
const delta = (value: number) => `${value > 0 ? '+' : value < 0 ? '−' : ''}${grams(Math.abs(value))}`
const tray = (event: SpoolEvent) => (event.ams_id != null && event.tray_id != null ? `AMS ${String.fromCharCode(65 + event.ams_id)}${event.tray_id + 1}` : null)

/** The short text after an event's kind: its grams, where it went, how it was dried. */
function detail(event: SpoolEvent, names: EventNames): string {
  const data = event.data as {
    temp_c?: number | null
    hours?: string | number | null
    reason?: string | null
    from?: string | null
    to?: string | null
    bb_status?: string | null
    reading_g?: number | null
    empty_g?: number | null
    drift_g?: number | null
    resolution?: string | null
    shortfall_g?: number | null
  }
  const source = event.source ? SOURCE[event.source] : undefined
  const parts: (string | null | undefined)[] = []
  switch (event.kind) {
    case 'weighed':
    case 'ams_reading':
      parts.push(event.grams_after != null ? grams(event.grams_after) : null, source)
      if (data.reading_g != null) {
        // #7682: the breakdown and the drift against what the ledger expected.
        parts.push(`reading ${grams(data.reading_g)}`, data.empty_g != null && data.resolution !== 'tared' ? `spool ${grams(data.empty_g)}` : null)
        // #7683: how a new spool's reading under its label was read.
        if (data.resolution === 'tared') parts.push('scale tared on the spool')
        else if (data.resolution === 'short') parts.push(`spool short by ${grams(data.shortfall_g ?? 0)}`)
        else if (data.resolution === 'label_wrong') parts.push('label corrected')
        if (data.drift_g != null) parts.push(Math.abs(data.drift_g) < 1 ? 'matches the ledger' : `${grams(Math.abs(data.drift_g))} ${data.drift_g < 0 ? 'less' : 'more'} than expected`)
      } else {
        parts.push(event.delta_g ? `${grams(Math.abs(event.delta_g))} ${event.delta_g < 0 ? 'less' : 'more'} than the last value` : null)
      }
      break
    case 'print_started':
    case 'print_ended':
      parts.push(event.delta_g != null ? delta(event.delta_g) : null, event.grams_after != null ? `${grams(event.grams_after)} left` : null, source, data.bb_status && data.bb_status !== 'completed' ? data.bb_status : null, tray(event))
      break
    case 'created':
      parts.push(event.grams_after != null ? `with ${grams(event.grams_after)}` : null)
      break
    case 'loaded':
    case 'unloaded':
      parts.push(tray(event))
      break
    case 'dried':
      parts.push(data.temp_c != null ? `${data.temp_c} °C${data.hours != null ? ` for ${data.hours} h` : ''}` : data.hours != null ? `for ${data.hours} h` : null)
      break
    case 'edited':
      parts.push(fromTypeText(event, names))
      break
  }
  if (data.reason) parts.push(data.reason.replace(/_/g, ' '))
  return parts.filter(Boolean).join(', ')
}

/** An id's name, as a link to its page when `hrefs` has one (#6869); any other value as text. */
function Ref({ field, value, names, hrefs }: { field: string; value: unknown; names: EventNames; hrefs: ReadonlyMap<string, string> }) {
  const text = valueText(field, value, names)
  const href = typeof value === 'string' && field.endsWith('_id') ? hrefs.get(value) : undefined
  return href ? <EntityLink to={href}>{text}</EntityLink> : text
}

/** A move's places, each linked to the spools in it when it has a page (#6869). */
function moveText(event: SpoolEvent, names: EventNames, hrefs: ReadonlyMap<string, string>): ReactNode {
  const data = event.data as { from?: string | null; to?: string | null }
  const place = (id: string | null | undefined) => {
    if (!id) return 'nowhere'
    const name = names.get(id)
    if (!name) return 'a place no longer listed'
    const href = hrefs.get(id)
    return href ? <EntityLink to={href}>{name}</EntityLink> : name
  }
  return (
    <>
      from {place(data.from)} to {place(data.to)}
    </>
  )
}

/** The dot on the rail: readings in the accent and prints in ink, as the grams chart draws its anchors and plate ticks; the rest recessive. */
const dot = (kind: SpoolEvent['kind']) =>
  kind === 'weighed' || kind === 'ams_reading' ? 'bg-primary' : kind === 'print_started' || kind === 'print_ended' ? 'bg-foreground' : 'bg-muted-foreground/50'

interface Props {
  /** Newest first, as the history route sends them; `null` while they load. */
  rows: readonly TimelineRow[] | null
  /** Every catalog's rows and the places, by id and name, for the ids an event names. */
  catalogs: readonly (readonly { id: string; name: string }[])[]
  types: readonly { id: string; code: string }[]
  /** Each id's page (a type, a place's spools, a catalog), for the ids an event names (#6869). */
  hrefs?: ReadonlyMap<string, string>
}

const NO_HREFS: ReadonlyMap<string, string> = new Map()

/**
 * #6269 — a spool's timeline (spec §5.7): every ledger event, newest first, with what
 * changed and who made it, the print and order each plate links to, and a filter by kind.
 */
export function SpoolTimeline({ rows, catalogs, types, hrefs = NO_HREFS }: Props) {
  const [filter, setFilter] = useState<Filter>('All')
  const names = useMemo(() => eventNames(catalogs, types), [catalogs, types])
  if (rows === null) return <p className="text-[13px] text-muted-foreground">Loading the history…</p>
  if (rows.length === 0) return <p className="text-[13px] text-muted-foreground">Nothing has happened to this spool yet.</p>
  const kinds = FILTERS.find((one) => one.label === filter)!.kinds as readonly SpoolEvent['kind'][] | null
  const shown = kinds ? rows.filter((row) => kinds.includes(row.event.kind)) : rows
  return (
    <div className="flex flex-col gap-3">
      <div role="group" aria-label="Show events" className="flex flex-wrap gap-1.5">
        {FILTERS.map((one) => (
          <Button key={one.label} type="button" size="xs" variant="outline" aria-pressed={filter === one.label} onClick={() => setFilter(one.label)} className="aria-pressed:bg-secondary aria-pressed:text-secondary-foreground">
            {one.label}
          </Button>
        ))}
      </div>
      {shown.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No events of this kind yet.</p>
      ) : (
        <ol aria-label="History of this spool" className="flex flex-col border-l text-[13px]">
          {shown.map(({ event, order_id: orderId, archive_id: archiveId }) => {
            const lines = event.kind === 'edited' ? changeEntries((event.data as { changes?: unknown }).changes) : []
            const move = event.kind === 'moved' ? moveText(event, names, hrefs) : null
            const text = detail(event, names)
            return (
              <li key={event.id} className="relative grid grid-cols-1 gap-x-4 gap-y-0.5 py-2 pl-4 sm:grid-cols-[11rem_minmax(0,1fr)_auto]">
                <span aria-hidden="true" className={`absolute top-[0.85rem] -left-[4.5px] size-2 rounded-full ring-2 ring-background ${dot(event.kind)}`} />
                <time dateTime={event.at} className="text-muted-foreground tabular-nums">
                  {when(event.at)}
                </time>
                <div className="flex min-w-0 flex-col gap-0.5">
                  <p className="break-words">
                    <span className="font-medium">{KIND_TEXT[event.kind] ?? event.kind.replace(/_/g, ' ')}</span>
                    {move && <span className="text-muted-foreground">, {move}</span>}
                    {text && <span className="text-muted-foreground tabular-nums">{`, ${text}`}</span>}
                  </p>
                  {lines.length > 0 && (
                    <ul aria-label="Changes" className="flex flex-col text-[12px]">
                      {lines.map((line, i) => (
                        <li key={i} className="break-words tabular-nums">
                          {line.whole ?? (
                            <>
                              {line.label}:{' '}
                              {line.before !== undefined && (
                                <>
                                  <Ref field={line.field} value={line.before} names={names} hrefs={hrefs} />
                                  {' → '}
                                </>
                              )}
                              <Ref field={line.field} value={line.after} names={names} hrefs={hrefs} />
                            </>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                  {(orderId || archiveId != null) && (
                    <p className="flex flex-wrap gap-x-3 text-[12px]">
                      {archiveId != null && (
                        <Link to={`/prints/${archiveId}`} className="underline underline-offset-2 hover:text-foreground">
                          Print {archiveId}
                        </Link>
                      )}
                      {orderId && (
                        <Link to={`/orders/${encodeURIComponent(orderId)}`} className="underline underline-offset-2 hover:text-foreground">
                          Its order
                        </Link>
                      )}
                    </p>
                  )}
                </div>
                <span className="text-[12px] text-muted-foreground sm:text-right">{actorText(event)}</span>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}
