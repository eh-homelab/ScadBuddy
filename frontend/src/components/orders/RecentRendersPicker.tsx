import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api/client'
import type { CustomizerSchema, ModelSummary, Output, RecentOutputsQuery } from '../../api/types'
import { timeAgo } from '../../lib/format'
import { diffFromDefaults } from '../../lib/params'
import { useAsync } from '../../lib/useAsync'
import { useDebounced } from '../../lib/useDebounced'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

/** One picked render and the quantity typed for it. */
export interface RenderPick {
  output: Output
  quantity: string
}

type Age = 'any' | '24h' | '7d' | '30d' | 'custom'

const AGES: { value: Age; label: string; hours?: number }[] = [
  { value: 'any', label: 'Any time' },
  { value: '24h', label: '24 h', hours: 24 },
  { value: '7d', label: '7 d', hours: 24 * 7 },
  { value: '30d', label: '30 d', hours: 24 * 30 },
  { value: 'custom', label: 'Custom' },
]
const ANY = 'any'
/** How many renders a page lists; "Load more" adds as many again. */
const PAGE = 50
/** The backend's `MAX_PAGE_LIMIT`. */
const MOST = 500
const TYPING_MS = 250

/** A day picked in a date field, as the ISO time its local midnight is. */
function dayStart(day: string, plusDays = 0): string | undefined {
  if (!day) return undefined
  const at = new Date(`${day}T00:00:00`)
  at.setDate(at.getDate() + plusDays)
  return at.toISOString()
}

/** The ISO time `hours` ago, or nothing for no window. */
function windowStart(hours: number | undefined): string | undefined {
  return hours === undefined ? undefined : new Date(Date.now() - hours * 3600 * 1000).toISOString()
}

function valueText(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

/**
 * The Add item dialog's "Recent renders" (#4777, order operations spec 2026-11-07 §3.1):
 * every model's saved outputs, newest first (`GET /outputs`), filtered by age (24 h, 7 d,
 * 30 d or a custom range), model, tag, text and whether an order has them. Each row shows
 * its thumbnail, model, name, the parameters that differ from the model's defaults and its
 * age; several may be picked, each with a quantity.
 */
export function RecentRendersPicker({
  open,
  models,
  picks,
  onPicks,
  disabled,
}: {
  open: boolean
  models: readonly ModelSummary[]
  picks: ReadonlyMap<string, RenderPick>
  onPicks: (next: Map<string, RenderPick>) => void
  disabled?: boolean
}) {
  const [age, setAge] = useState<Age>('any')
  // The start of a 24 h, 7 d or 30 d window, taken when it is picked.
  const [since, setSince] = useState<string | undefined>(undefined)
  const [fromDay, setFromDay] = useState('')
  const [toDay, setToDay] = useState('')
  const [slug, setSlug] = useState(ANY)
  const [tag, setTag] = useState('')
  const [text, setText] = useState('')
  const [ordered, setOrdered] = useState<typeof ANY | 'yes' | 'no'>(ANY)
  const [limit, setLimit] = useState(PAGE)
  const typedTag = useDebounced(tag.trim(), TYPING_MS)
  const typedText = useDebounced(text.trim(), TYPING_MS)

  const filters = useMemo<RecentOutputsQuery>(() => {
    const range = age === 'custom' ? { from: dayStart(fromDay), to: dayStart(toDay, 1) } : { from: since }
    return {
      ...range,
      ...(slug !== ANY ? { slug: [slug] } : {}),
      ...(typedTag ? { tag: typedTag } : {}),
      ...(typedText ? { q: typedText } : {}),
      ...(ordered !== ANY ? { ordered } : {}),
    }
  }, [age, since, fromDay, toDay, slug, typedTag, typedText, ordered])
  // Other filters start from the first page again.
  useEffect(() => setLimit(PAGE), [filters])

  const listed = useAsync(
    () => (open ? api.listRecentOutputs(filters, { limit }) : Promise.resolve({ items: [] as Output[], total: 0 })),
    [open, filters, limit],
  )
  // The last answer stays on screen while another filter's loads, so the list never blinks.
  const [lastPage, setLastPage] = useState(listed.data)
  if (listed.data && listed.data !== lastPage) setLastPage(listed.data)
  const page = listed.data ?? (listed.loading ? lastPage : undefined)
  const rows = useMemo(() => page?.items ?? [], [page])

  // Each listed model's schema once, for the parameters that differ from its defaults.
  const [schemas, setSchemas] = useState<Record<string, CustomizerSchema | null>>({})
  const wanted = useMemo(() => [...new Set(rows.map((o) => o.slug))].filter((s) => !(s in schemas)), [rows, schemas])
  useEffect(() => {
    if (wanted.length === 0) return
    let live = true
    for (const one of wanted) {
      api.getSchema(one).then(
        (schema) => live && setSchemas((known) => ({ ...known, [one]: schema as CustomizerSchema })),
        () => live && setSchemas((known) => ({ ...known, [one]: null })),
      )
    }
    return () => {
      live = false
    }
  }, [wanted])

  const nameOf = (s: string) => models.find((m) => m.slug === s)?.name ?? s

  function pickAge(next: Age, hours: number | undefined) {
    setAge(next)
    setSince(windowStart(hours))
  }

  function toggle(output: Output, on: boolean) {
    const next = new Map(picks)
    if (on) next.set(output.id, { output, quantity: '1' })
    else next.delete(output.id)
    onPicks(next)
  }

  function setQuantity(id: string, quantity: string) {
    const pick = picks.get(id)
    if (!pick) return
    onPicks(new Map(picks).set(id, { ...pick, quantity }))
  }

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap gap-1" role="group" aria-label="Saved">
        {AGES.map((a) => (
          <Button key={a.value} type="button" size="sm" variant={age === a.value ? 'secondary' : 'outline'} aria-pressed={age === a.value} onClick={() => pickAge(a.value, a.hours)}>
            {a.label}
          </Button>
        ))}
      </div>
      {age === 'custom' && (
        <div className="flex flex-wrap gap-3">
          <div className="flex flex-col gap-1">
            <Label htmlFor="recent-from">From</Label>
            <Input id="recent-from" type="date" value={fromDay} onChange={(e) => setFromDay(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="recent-to">To</Label>
            <Input id="recent-to" type="date" value={toDay} onChange={(e) => setToDay(e.target.value)} />
          </div>
        </div>
      )}
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Select value={slug} onValueChange={setSlug}>
          <SelectTrigger aria-label="Model">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Any model</SelectItem>
            {models.map((m) => (
              <SelectItem key={m.slug} value={m.slug}>
                {m.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select value={ordered} onValueChange={(v) => setOrdered(v as typeof ordered)}>
          <SelectTrigger aria-label="On an order">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>On an order or not</SelectItem>
            <SelectItem value="yes">On an order</SelectItem>
            <SelectItem value="no">Not on an order</SelectItem>
          </SelectContent>
        </Select>
        <Input aria-label="Tag" placeholder="Tag" maxLength={200} value={tag} onChange={(e) => setTag(e.target.value)} />
        <Input aria-label="Search renders" type="search" placeholder="Name or setting" maxLength={256} value={text} onChange={(e) => setText(e.target.value)} />
      </div>
      {listed.error ? (
        <p className="text-xs text-warn">Could not list the renders: {listed.error.message}</p>
      ) : !page ? (
        <p className="text-[13px] text-muted-foreground">Loading renders…</p>
      ) : rows.length === 0 ? (
        <p className="text-[13px] text-muted-foreground">No renders match.</p>
      ) : (
        <ul aria-label="Recent renders" className="flex max-h-72 flex-col overflow-y-auto rounded-md border">
          {rows.map((output) => {
            const name = output.name || 'Untitled output'
            const pick = picks.get(output.id)
            const schema = schemas[output.slug]
            const diff = schema ? diffFromDefaults(schema, output.params ?? {}) : []
            return (
              <li key={output.id} data-testid={`recent-${output.id}`} className="flex items-start gap-2 border-b px-2 py-1.5 last:border-b-0">
                <Checkbox
                  aria-label={name}
                  className="mt-1"
                  checked={pick !== undefined}
                  disabled={disabled}
                  onCheckedChange={(on) => toggle(output, on === true)}
                />
                {output.has_thumbnail ? (
                  <img src={api.outputThumbnailUrl(output.id, output.thumbnail_version)} alt="" className="size-10 shrink-0 rounded border object-contain" loading="lazy" />
                ) : (
                  <span aria-hidden="true" className="size-10 shrink-0 rounded border bg-muted" />
                )}
                <span className="flex min-w-0 flex-1 flex-col text-sm">
                  <span className="truncate">
                    <span className="font-medium">{name}</span>
                    <span className="text-muted-foreground"> · {nameOf(output.slug)}</span>
                  </span>
                  <span className="truncate text-xs text-muted-foreground">
                    {diff.length > 0 ? diff.map((d) => `${d.caption}: ${valueText(d.value)}`).join(', ') : schema ? 'Model defaults' : ''}
                  </span>
                  <span className="text-xs text-muted-foreground">saved {timeAgo(output.created_at)}</span>
                </span>
                {pick && (
                  <Input
                    aria-label={`Quantity of ${name}`}
                    inputMode="numeric"
                    className="h-8 w-16"
                    disabled={disabled}
                    value={pick.quantity}
                    onChange={(e) => setQuantity(output.id, e.target.value)}
                  />
                )}
              </li>
            )
          })}
        </ul>
      )}
      {page && page.total > rows.length && limit < MOST && (
        <Button type="button" variant="ghost" size="sm" className="self-start" onClick={() => setLimit((n) => Math.min(n + PAGE, MOST))}>
          Load more ({page.total - rows.length} more)
        </Button>
      )}
    </div>
  )
}
