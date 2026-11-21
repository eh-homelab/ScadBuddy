import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { ApiError } from '../../api/client'
import { getTag, listSpools, listTags, type TagDetail, type TagRow } from '../../api/filament'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { TD, TH, when } from '../../components/filament/formClasses'
import { KINDS } from '../../components/filament/tagKinds'
import { spoolPath } from '../../components/filament/spoolsQuery'
import { Button } from '@/components/ui/button'

const ANY = '__any'

const kindName = (kind: string) => KINDS.find((one) => one.id === kind)?.name ?? kind

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The tags could not be loaded. Try again.')

const SHOWN = ['all', 'bound', 'unbound'] as const

/** A tag's key as a person finds it printed: the UID, else the tray UUID. */
const keyOf = (tag: Pick<TagRow, 'tag_uid' | 'tray_uuid'>) => tag.tag_uid ?? tag.tray_uuid ?? ''

/**
 * #6262 — the Tags tab: every RFID tag the spools have had, the spool each is on now, since
 * when, and whether it was taken over from another spool. A row opens the tag's history,
 * newest first. The kind and bound filters are in the URL.
 */
export function TagsTab() {
  const [params, setParams] = useSearchParams()
  const kind = params.get('kind') ?? ''
  const shown = SHOWN.find((one) => one === params.get('show')) ?? 'all'
  const [tags, setTags] = useState<TagRow[] | null>(null)
  // Spool numbers by id: read each time a history opens (a row already carries its spool's number).
  const [numbers, setNumbers] = useState<Map<string, number> | null>(null)
  const asked = useRef(0)
  const historyRef = useRef<HTMLElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [open, setOpen] = useState<TagDetail | null>(null)
  const [historyError, setHistoryError] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      setTags(await listTags({ includeUnbound: true }))
      setError(null)
    } catch (cause) {
      setError(message(cause))
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const change = (name: string, value: string, fallback: string) => {
    const next = new URLSearchParams(params)
    if (value === fallback) next.delete(name)
    else next.set(name, value)
    setParams(next, { replace: true })
  }

  const rows = useMemo(
    () => (tags ?? []).filter((tag) => (!kind || tag.kind === kind) && (shown === 'all' || (shown === 'bound') === (tag.binding != null))),
    [tags, kind, shown],
  )

  useEffect(() => {
    if (open) historyRef.current?.focus()
  }, [open])

  async function openHistory(tag: TagRow) {
    const mine = ++asked.current
    setHistoryError(null)
    try {
      const detail = await getTag(tag.id)
      // A history the numbers cannot name still lists its spools, as "Spool".
      // Read at each opening: a spool made since is named too.
      const known = await listSpools({ includeArchived: true }).then((spools) => new Map(spools.map((spool) => [spool.id, spool.number])), () => numbers)
      if (mine !== asked.current) return
      if (known) setNumbers(known)
      setOpen(detail)
    } catch (cause) {
      if (mine !== asked.current) return
      setOpen(null)
      setHistoryError(message(cause))
    }
  }

  const spoolLink = (id: string, number: number | null | undefined) => (
    <Link to={spoolPath({ id })} className="font-medium underline-offset-4 hover:underline">
      {number == null ? 'Spool' : `#${number}`}
    </Link>
  )

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3 text-[13px]">
        <label className="flex items-center gap-2 text-muted-foreground">
          Kind
          <Select value={kind || ANY} onValueChange={(next) => change('kind', next === ANY ? '' : next, '')}>
            <SelectTrigger aria-label="Kind" className="h-8 w-auto text-[13px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ANY}>Every kind</SelectItem>
              {KINDS.map((one) => (
                <SelectItem key={one.id} value={one.id}>
                  {one.name}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
        <label className="flex items-center gap-2 text-muted-foreground">
          Show
          <Select value={shown} onValueChange={(next) => change('show', next, 'all')}>
            <SelectTrigger aria-label="Show" className="h-8 w-auto text-[13px]">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All tags</SelectItem>
              <SelectItem value="bound">On a spool</SelectItem>
              <SelectItem value="unbound">Not on a spool</SelectItem>
            </SelectContent>
          </Select>
        </label>
      </div>
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
      {!tags && !error && <p className="text-[13px] text-muted-foreground">Loading tags…</p>}
      {tags && (
        <div className="overflow-x-auto rounded-md border">
          <table aria-label="Tags" className="w-full text-left text-[13px]">
            <thead className="bg-muted/50 text-muted-foreground">
              <tr>
                <th scope="col" className={TH}>Tag</th>
                <th scope="col" className={TH}>Kind</th>
                <th scope="col" className={TH}>Spool</th>
                <th scope="col" className={TH}>Bound since</th>
                <th scope="col" className={TH}>Taken over</th>
                <th scope="col" className={TH}>
                  <span className="sr-only">History</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((tag) => (
                <tr key={tag.id} data-tag={tag.id} className="border-t">
                  <td className={`${TD} font-mono text-[12px]`}>{keyOf(tag)}</td>
                  <td className={TD}>{kindName(tag.kind)}</td>
                  <td className={TD}>
                    {tag.binding ? spoolLink(tag.binding.spool_id, tag.spool_number) : <span className="text-muted-foreground">Not on a spool</span>}
                  </td>
                  <td className={TD}>
                    {tag.binding ? <time dateTime={tag.binding.bound_at}>{when(tag.binding.bound_at)}</time> : ''}
                  </td>
                  <td className={TD}>{tag.binding ? (tag.binding.override ? 'Yes' : 'No') : ''}</td>
                  <td className={`${TD} text-right`}>
                    <Button type="button" size="xs" variant="outline" aria-label={`History of ${keyOf(tag)}`} onClick={() => void openHistory(tag)}>
                      History
                    </Button>
                  </td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr className="border-t">
                  <td colSpan={6} className="px-2 py-3 text-muted-foreground">
                    No tags match. A spool&apos;s page binds a tag to it.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
      {historyError && (
        <p role="alert" className="text-[13px] text-warn">
          {historyError}
        </p>
      )}
      {open && (
        <section ref={historyRef} tabIndex={-1} aria-label="Tag history" className="flex flex-col gap-2 rounded-md border p-3 outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold tracking-tight">History of {keyOf(open)}</h2>
            <Button
              type="button"
              size="xs"
              variant="ghost"
              onClick={() => {
                // An answer still on its way must not reopen what was just closed.
                asked.current += 1
                setOpen(null)
              }}
            >
              Close
            </Button>
          </div>
          <ul className="flex flex-col gap-1 text-[13px]">
            {(open.history ?? []).map((binding) => (
              <li key={binding.id} className="flex flex-wrap items-baseline gap-x-2">
                {spoolLink(binding.spool_id, numbers?.get(binding.spool_id))}
                <span className="text-muted-foreground">
                  <time dateTime={binding.bound_at}>{when(binding.bound_at)}</time>
                  {' to '}
                  {binding.unbound_at ? <time dateTime={binding.unbound_at}>{when(binding.unbound_at)}</time> : 'now'}
                </span>
                {binding.override && <span className="text-muted-foreground">Taken over</span>}
                {binding.reason && <span>{binding.reason}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
