import { useEffect, useId, useMemo, useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from 'react-router'
import { ArrowDownIcon, ArrowUpIcon, ImageIcon, MoreHorizontalIcon } from 'lucide-react'
import { api, ApiError } from '../api/client'
import type { StorefrontListingRow } from '../api/types'
import { DataTable, type Column } from '../components/records/DataTable'
import { FilterBar } from '../components/records/FilterBar'
import {
  DEFAULT_LISTINGS_QUERY,
  isFiltered,
  listingPath,
  PAGE_SIZE,
  parseListingsQuery,
  toApiParams,
  toListingsParams,
  type ListingsQuery,
  type ListingsSort,
} from '../components/storefront/listingsQuery'
import { useAsync } from '../lib/useAsync'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'

/** The Select's value for "no filter": Radix refuses an empty value. */
const ANY = '__any'

type BulkAction = 'publish' | 'unpublish' | 'delete'

const BULK_VERB: Record<BulkAction, { doing: string; done: string; button: string }> = {
  publish: { doing: 'Publishing', done: 'Published', button: 'Publish' },
  unpublish: { doing: 'Unpublishing', done: 'Unpublished', button: 'Unpublish' },
  delete: { doing: 'Deleting', done: 'Deleted', button: 'Delete' },
}

/** One bulk run: how far it is, and each listing a refusal stopped. */
interface BulkRun {
  action: BulkAction
  done: number
  total: number
  failed: { id: string; title: string; text: string }[]
  running: boolean
}

function message(error: unknown): string {
  return error instanceof ApiError || error instanceof Error ? error.message : String(error)
}


const UPDATED = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })

/**
 * `/storefront` (#5282, spec 2026-11-02 §5.3; #6157) — the public site's listings as a table
 * that stays quick with thousands: the server searches (title, model), filters (status,
 * model, material), sorts and pages them, 50 at a time, and the URL holds all of it. Rows
 * are selected for a bulk Publish, Unpublish or Delete, and moved by "Move to top" or
 * "Move to position…", never dragged. A listing opens in its own editor route
 * (`/storefront/listings/<id>`), whose "Back to listings" returns to the same page.
 *
 * The old addresses still work: `?listing=<id>` (the assistant's link) opens that
 * listing's editor, and `?sell=…` ("Sell on storefront") opens a new one filled in.
 */
export function StorefrontPage() {
  const [params, setParams] = useSearchParams()
  const location = useLocation()
  const navigate = useNavigate()
  const query = useMemo(() => parseListingsQuery(params), [params])
  const apiParams = toApiParams(query).toString()
  const page = useAsync(() => api.storefrontListings(new URLSearchParams(apiParams)), [apiParams])
  const models = useAsync(() => api.listModels(), [])
  const book = useAsync(() => api.priceBook(), [])
  const settings = useAsync(() => api.getSettings(), [])
  const [requests, setRequests] = useState<boolean | null>(null)
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set())
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [bulk, setBulk] = useState<BulkRun | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [moving, setMoving] = useState<StorefrontListingRow | null>(null)
  const requestsId = useId()

  const rows = useMemo(() => page.data?.items ?? [], [page.data])
  const total = page.data?.total ?? 0
  /** How many listings the shop has: the last place "Move to position…" can name, whatever the filter. */
  const places = page.data?.places ?? 0
  const lastPage = Math.max(1, Math.ceil(total / PAGE_SIZE))
  const modelName = useMemo(() => new Map((models.data ?? []).map((model) => [model.slug, model.name])), [models.data])
  const materials = useMemo(() => [...new Set((book.data?.filaments ?? []).map((f) => f.material.toUpperCase()))].sort(), [book.data])

  // A selection is of the rows shown: a new page, search or filter clears it.
  useEffect(() => setSelected(new Set()), [apiParams])

  // A page past the last (its rows deleted, or an old link) moves to the last page, so the
  // list never reads empty while listings match.
  const loaded = page.data
  useEffect(() => {
    if (!loaded || loaded.items.length > 0 || loaded.total === 0 || query.page <= lastPage) return
    setParams(toListingsParams({ ...query, page: lastPage }), { replace: true })
  }, [loaded, query, lastPage, setParams])

  // "/" puts the cursor in the search, as on other lists; never while typing elsewhere.
  useEffect(() => {
    function onKey(event: KeyboardEvent) {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return
      const target = event.target as HTMLElement | null
      if (target && (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName))) return
      const search = document.querySelector<HTMLInputElement>('[data-testid="storefront-page"] input[type="search"]')
      if (!search) return
      event.preventDefault()
      search.focus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  // The old links: `?listing=<id>` and `?sell=…` open the editor.
  const legacyListing = params.get('listing')
  const sell = params.get('sell')
  if (legacyListing) return <Navigate replace to={listingPath(legacyListing)} />
  if (sell) return <Navigate replace to={`/storefront/listings/new?${new URLSearchParams({ sell })}`} />

  /** The list at `next`; `typing` replaces the history entry, so each key is not a step back. */
  function onChange(next: ListingsQuery, { typing = false }: { typing?: boolean } = {}) {
    setParams(toListingsParams(next), { replace: typing })
  }

  function sortBy(sort: ListingsSort) {
    const dir = query.sort === sort && query.dir === 'asc' ? 'desc' : 'asc'
    onChange({ ...query, sort, dir, page: 1 })
  }

  async function act(run: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try {
      await run()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }

  async function moveTo(row: StorefrontListingRow, place: number) {
    await act(async () => {
      await api.moveStorefrontListing(row.id, place)
      page.reload()
    })
  }

  async function runBulk(action: BulkAction) {
    const chosen = rows.filter((row) => selected.has(row.id))
    const run: BulkRun = { action, done: 0, total: chosen.length, failed: [], running: true }
    setBulk(run)
    setError(null)
    // One at a time: a publish holds a lock of its own, and the server refuses past two at once.
    for (const row of chosen) {
      try {
        if (action === 'publish') await api.publishStorefrontListing(row.id)
        else if (action === 'unpublish') await api.unpublishStorefrontListing(row.id)
        else await api.deleteStorefrontListing(row.id)
      } catch (caught) {
        run.failed = [...run.failed, { id: row.id, title: row.title, text: message(caught) }]
      }
      run.done += 1
      setBulk({ ...run })
    }
    setBulk({ ...run, running: false })
    setSelected(new Set(run.failed.map((f) => f.id)))
    page.reload()
  }

  async function setRequestsEnabled(on: boolean) {
    setRequests(on)
    await act(async () => {
      try {
        const next = await api.putSettings({ storefront_requests_enabled: on })
        setRequests(next.storefront_requests_enabled)
      } catch (caught) {
        setRequests(null)
        throw caught
      }
    })
  }

  const back = { back: location.search }
  const allShown = rows.length > 0 && rows.every((row) => selected.has(row.id))
  const someShown = rows.some((row) => selected.has(row.id))
  const bulkRunning = bulk?.running === true
  const requestsOn = requests ?? settings.data?.storefront_requests_enabled ?? false
  const sortState = (sort: ListingsSort) => (query.sort === sort ? (query.dir === 'asc' ? 'ascending' : 'descending') : 'none')

  /** A column's header: a button that sorts by it, and again the other way. */
  function sortHeader(sort: ListingsSort, label: string) {
    const state = sortState(sort)
    return (
      <button type="button" className="inline-flex items-center gap-1 hover:text-foreground" onClick={() => sortBy(sort)}>
        {label}
        {state === 'ascending' && <ArrowUpIcon aria-hidden="true" className="size-3" />}
        {state === 'descending' && <ArrowDownIcon aria-hidden="true" className="size-3" />}
      </button>
    )
  }

  const columns: Column<StorefrontListingRow>[] = [
    {
      key: 'select',
      header: (
        <Checkbox
          aria-label="Select every listing on this page"
          checked={allShown ? true : someShown ? 'indeterminate' : false}
          disabled={rows.length === 0 || bulkRunning}
          onCheckedChange={(on) => setSelected(on === true ? new Set(rows.map((row) => row.id)) : new Set())}
        />
      ),
      headerClassName: 'w-8',
      cell: (row) => (
        <Checkbox
          aria-label={`Select ${row.title}`}
          checked={selected.has(row.id)}
          disabled={bulkRunning}
          onCheckedChange={(on) =>
            setSelected((cur) => {
              const next = new Set(cur)
              if (on === true) next.add(row.id)
              else next.delete(row.id)
              return next
            })
          }
        />
      ),
    },
    {
      key: 'picture',
      header: <span className="sr-only">Picture</span>,
      headerClassName: 'w-12',
      className: 'py-1',
      cell: (row) =>
        row.media_id ? (
          <img src={api.mediaUrl(row.slug, { id: row.media_id })} alt="" loading="lazy" className="size-8 rounded border object-cover" />
        ) : (
          <span className="flex size-8 items-center justify-center rounded border bg-muted text-muted-foreground">
            <ImageIcon aria-hidden="true" className="size-4" />
          </span>
        ),
    },
    {
      key: 'title',
      header: sortHeader('title', 'Title'),
      sort: sortState('title'),
      className: 'max-w-[24rem]',
      cell: (row) => (
        <Link to={listingPath(row.id)} state={back} className="block truncate font-medium hover:underline" title={row.title}>
          {row.title}
        </Link>
      ),
    },
    {
      key: 'model',
      header: sortHeader('slug', 'Model'),
      sort: sortState('slug'),
      className: 'max-w-[14rem] truncate text-muted-foreground',
      cell: (row) => <span title={row.slug}>{modelName.get(row.slug) ?? row.slug}</span>,
    },
    {
      key: 'status',
      header: 'Status',
      cell: (row) => (row.published ? <Badge>Published</Badge> : <Badge variant="outline">Draft</Badge>),
    },
    {
      key: 'place',
      header: sortHeader('position', 'Place'),
      sort: sortState('position'),
      className: 'sb-num text-right tabular-nums',
      headerClassName: 'text-right',
      cell: (row) => row.place,
    },
    {
      key: 'updated',
      header: sortHeader('updated', 'Updated'),
      sort: sortState('updated'),
      className: 'whitespace-nowrap text-muted-foreground',
      cell: (row) => <time dateTime={row.updated_at}>{UPDATED.format(new Date(row.updated_at))}</time>,
    },
    {
      key: 'actions',
      header: <span className="sr-only">Actions</span>,
      headerClassName: 'w-10',
      cell: (row) => (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon-sm" aria-label={`Actions for ${row.title}`} disabled={busy || bulkRunning}>
              <MoreHorizontalIcon />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={() => void navigate(listingPath(row.id), { state: back })}>Edit</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem disabled={row.place === 1} onSelect={() => void moveTo(row, 1)}>
              Move to top
            </DropdownMenuItem>
            <DropdownMenuItem onSelect={() => setMoving(row)}>Move to position…</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      ),
    },
  ]

  const filtered = isFiltered(query)
  const first = total === 0 ? 0 : (query.page - 1) * PAGE_SIZE + 1
  const last = Math.min(query.page * PAGE_SIZE, total)

  return (
    <div className="h-full overflow-y-auto" data-testid="storefront-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h1 className="text-xl font-semibold tracking-tight">Storefront</h1>
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <Switch id={requestsId} checked={requestsOn} disabled={!settings.data} onCheckedChange={(on) => void setRequestsEnabled(on)} />
              <Label htmlFor={requestsId} className="font-normal">
                Ask for a design
              </Label>
            </div>
            <Button asChild>
              <Link to="/storefront/listings/new" state={back}>
                New listing
              </Link>
            </Button>
          </div>
        </div>
        <FilterBar
          label="Listing filters"
          search={{ value: query.q, onChange: (q) => onChange({ ...query, q, page: 1 }, { typing: true }), placeholder: 'Search title or model' }}
          selects={[
            {
              key: 'status',
              label: 'Status',
              value: query.status || ANY,
              options: [
                { value: ANY, label: 'Any status' },
                { value: 'published', label: 'Published' },
                { value: 'draft', label: 'Draft' },
              ],
              onChange: (status) => onChange({ ...query, status: status === ANY ? '' : (status as ListingsQuery['status']), page: 1 }),
            },
            {
              key: 'model',
              label: 'Model',
              value: query.model || ANY,
              options: [
                { value: ANY, label: 'Any model' },
                ...(query.model && !modelName.has(query.model) ? [{ value: query.model, label: query.model }] : []),
                ...(models.data ?? []).map((model) => ({ value: model.slug, label: model.name })),
              ],
              onChange: (model) => onChange({ ...query, model: model === ANY ? '' : model, page: 1 }),
            },
            {
              key: 'material',
              label: 'Material',
              value: query.material || ANY,
              options: [
                { value: ANY, label: 'Any material' },
                ...(query.material && !materials.includes(query.material) ? [query.material] : []).map((m) => ({ value: m, label: m })),
                ...materials.map((m) => ({ value: m, label: m })),
              ],
              onChange: (material) => onChange({ ...query, material: material === ANY ? '' : material, page: 1 }),
            },
          ]}
          filtered={filtered}
          onClear={() => onChange({ ...DEFAULT_LISTINGS_QUERY, sort: query.sort, dir: query.dir })}
        />
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
          </p>
        )}
        {(someShown || bulk) && (
          <div role="region" aria-label="Selected listings" className="flex flex-wrap items-center gap-2 rounded-md border bg-muted/40 px-3 py-2 text-[13px]">
            {bulk?.running ? (
              <span role="status">
                {BULK_VERB[bulk.action].doing} {bulk.done} of {bulk.total}…
              </span>
            ) : (
              <>
                <span className="mr-2">{selected.size} selected</span>
                <Button size="sm" disabled={!someShown || busy} onClick={() => void runBulk('publish')}>
                  Publish
                </Button>
                <Button size="sm" variant="outline" disabled={!someShown || busy} onClick={() => void runBulk('unpublish')}>
                  Unpublish
                </Button>
                <Button size="sm" variant="destructive" disabled={!someShown || busy} onClick={() => setConfirmDelete(true)}>
                  Delete
                </Button>
                <Button size="sm" variant="ghost" disabled={!someShown} onClick={() => setSelected(new Set())}>
                  Clear selection
                </Button>
                {bulk && (
                  <span role="status" className="ml-auto">
                    {BULK_VERB[bulk.action].done} {bulk.done - bulk.failed.length} of {bulk.total}.
                    {bulk.failed.length > 0 && ` ${bulk.failed.length} refused.`}
                    <Button size="sm" variant="link" className="h-auto px-1" onClick={() => setBulk(null)}>
                      Dismiss
                    </Button>
                  </span>
                )}
              </>
            )}
            {bulk && bulk.failed.length > 0 && (
              <ul className="w-full list-disc pl-5 text-warn" aria-label="Refused">
                {bulk.failed.map((failure) => (
                  <li key={failure.id}>
                    {failure.title}: {failure.text}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {page.error ? (
          <div role="alert" className="flex flex-wrap items-center gap-2 rounded-md border border-warn/50 bg-warn/5 px-3 py-2 text-[13px]">
            <span>Could not load the listings: {page.error.message}</span>
            <Button size="sm" variant="outline" onClick={() => page.reload()}>
              Retry
            </Button>
          </div>
        ) : !page.data ? (
          <div role="status" aria-label="Loading listings" className="flex flex-col gap-2">
            {Array.from({ length: 8 }, (_, i) => (
              <Skeleton key={i} className="h-12 w-full" />
            ))}
          </div>
        ) : (
          <>
            <DataTable
              label="Listings"
              columns={columns}
              rows={rows}
              rowKey={(row) => row.id}
              rowTestId={() => 'storefront-listing'}
              rowClassName={() => '[&>td]:align-middle'}
              empty={
                filtered ? (
                  <>
                    No listing matches.{' '}
                    <Button variant="link" className="h-auto p-0" onClick={() => onChange({ ...DEFAULT_LISTINGS_QUERY, sort: query.sort, dir: query.dir })}>
                      Clear filters
                    </Button>
                  </>
                ) : (
                  'No listings yet. Open a design and choose Generate’s “Sell on storefront…”, or make one here with New listing.'
                )
              }
            />
            {total > 0 && (
              <nav aria-label="Pages" className="flex flex-wrap items-center justify-between gap-2 text-[13px]">
                <span className="sb-num text-muted-foreground" data-testid="storefront-range">
                  {first.toLocaleString()}–{last.toLocaleString()} of {total.toLocaleString()}
                </span>
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="outline" disabled={query.page <= 1} onClick={() => onChange({ ...query, page: query.page - 1 })}>
                    Previous
                  </Button>
                  <span className="sb-num">
                    Page {query.page} of {lastPage}
                  </span>
                  <Button size="sm" variant="outline" disabled={query.page >= lastPage} onClick={() => onChange({ ...query, page: query.page + 1 })}>
                    Next
                  </Button>
                </div>
              </nav>
            )}
          </>
        )}
      </div>

      <AlertDialog open={confirmDelete} onOpenChange={setConfirmDelete}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              Delete {selected.size} {selected.size === 1 ? 'listing' : 'listings'}?
            </AlertDialogTitle>
            <AlertDialogDescription>A published listing is refused: unpublish it first. A deleted listing cannot be brought back.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <Button
              variant="destructive"
              onClick={() => {
                setConfirmDelete(false)
                void runBulk('delete')
              }}
            >
              Delete
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <MoveDialog
        row={moving}
        places={places}
        onClose={() => setMoving(null)}
        onMove={(row, place) => {
          setMoving(null)
          void moveTo(row, place)
        }}
      />
    </div>
  )
}

/** "Move to position…": a place from 1, typed; past the end puts the listing last. */
function MoveDialog({
  row,
  places,
  onClose,
  onMove,
}: {
  row: StorefrontListingRow | null
  places: number
  onClose: () => void
  onMove: (row: StorefrontListingRow, place: number) => void
}) {
  const [text, setText] = useState('')
  const inputId = useId()
  useEffect(() => {
    if (row) setText(String(row.place))
  }, [row])
  const place = Number.parseInt(text, 10)
  const valid = Number.isInteger(place) && place >= 1
  return (
    <Dialog open={row !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>Move “{row?.title}”</DialogTitle>
        </DialogHeader>
        <form
          className="flex flex-col gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (row && valid) onMove(row, place)
          }}
        >
          <Label htmlFor={inputId}>New place in the shop (1 to {places.toLocaleString()})</Label>
          <Input id={inputId} type="number" inputMode="numeric" min={1} max={places} value={text} onChange={(event) => setText(event.target.value)} autoFocus />
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!valid}>
              Move
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
