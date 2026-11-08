import { useEffect, useId, useMemo, useState } from 'react'
import { ArrowDownIcon, ArrowUpIcon, GripVerticalIcon } from 'lucide-react'
import { api, ApiError } from '../api/client'
import type { StorefrontListing, StorefrontListingInput } from '../api/types'
import { ListingEditor } from '../components/storefront/ListingEditor'
import { defaultsFor, draftOf, emptyDraft, listingInput, previewListing, savedKey, startingValues, storedKey, unsaveable, type ListingDraft } from '../components/storefront/listingDraft'
import { ListingView, type ParamValues } from '../storefront/components/ListingView'
import { useAsync } from '../lib/useAsync'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

/** The listing open in the editor: a saved one (its id and the `updated_at` it was read at) or a new one. */
interface Open {
  /**
   * Which opening of a listing this is: kept through its edits, saves and publishes,
   * new each time a listing is opened. An answer is applied only while its opening is
   * still the open one, so a slow publish never puts back a listing the user left.
   */
  key: symbol
  id: string | null
  updatedAt: string | null
  published: boolean
  draft: ListingDraft
  /** What was last saved (`savedKey`), to tell whether the draft needs saving before a publish. */
  saved: string | null
}

function fresh(): Open {
  return { key: Symbol('listing'), id: null, updatedAt: null, published: false, draft: emptyDraft(), saved: null }
}

function opened(listing: StorefrontListing, key: symbol = Symbol('listing')): Open {
  const draft = draftOf(listing)
  return { key, id: listing.id, updatedAt: listing.updated_at, published: listing.published, draft, saved: storedKey(listing) }
}

/** What page-wide actions (the order, the design-request switch) are busy and refused under. */
const PAGE = Symbol('page')

function message(error: unknown): string {
  return error instanceof ApiError || error instanceof Error ? error.message : String(error)
}

/** `ids` with the one at `from` moved to `to`. */
function moved(ids: string[], from: number, to: number): string[] {
  const next = [...ids]
  const [id] = next.splice(from, 1)
  if (id !== undefined) next.splice(to, 0, id)
  return next
}

/**
 * `/storefront` (#5282, spec 2026-11-02 §5.3) — the public site's listings: the list in
 * the order the site shows them (drag a row, or move it up or down), the editor of the
 * open listing, Publish and Unpublish, the "Ask for a design" switch
 * (`storefront_requests_enabled`) and a live preview of the customer's view, the
 * storefront's own `ListingView` mounted with the unsaved listing.
 */
export function StorefrontPage() {
  const listings = useAsync(() => api.storefrontListings(), [])
  const models = useAsync(() => api.listModels(), [])
  const book = useAsync(() => api.priceBook(), [])
  const settings = useAsync(() => api.getSettings(), [])
  const [order, setOrder] = useState<StorefrontListing[] | null>(null)
  const [open, setOpen] = useState<Open | null>(null)
  /** Whose actions run: the open listing's key, another listing's, or `PAGE`. */
  const [busy, setBusy] = useState<ReadonlySet<symbol>>(new Set())
  /** The last refusal, and whose it is: shown only on that listing (or page-wide). */
  const [error, setError] = useState<{ key: symbol; text: string } | null>(null)
  const [requests, setRequests] = useState<boolean | null>(null)
  const [dragging, setDragging] = useState<number | null>(null)
  const [values, setValues] = useState<ParamValues | null>(null)
  const requestsId = useId()

  const rows = order ?? listings.data ?? []
  const draft = open?.draft
  const slug = draft?.slug ?? ''
  const commit = draft?.commit ?? ''
  const versions = useAsync(() => (slug ? api.listVersions(slug) : Promise.resolve([])), [slug])
  const schema = useAsync(() => (slug && commit ? api.getSchema(slug, commit) : Promise.resolve(undefined)), [slug, commit])
  const filaments = useMemo(() => book.data?.filaments ?? [], [book.data])
  const media = useMemo(() => models.data?.find((model) => model.slug === slug)?.media ?? [], [models.data, slug])

  // A new listing starts on the first template, at its current version, with the
  // whole of what the schema allows offered.
  useEffect(() => {
    if (!open || open.id !== null) return
    const first = models.data?.[0]
    if (!open.draft.slug && first) {
      setOpen({ ...open, draft: { ...open.draft, slug: first.slug, title: open.draft.title || first.name } })
    }
  }, [open, models.data])
  useEffect(() => {
    if (!open || open.draft.commit || !versions.data?.length) return
    const current = versions.data.find((v) => v.current) ?? versions.data[0]
    if (current) setOpen({ ...open, draft: { ...open.draft, commit: current.commit } })
  }, [open, versions.data])
  useEffect(() => {
    if (!open || open.id !== null || !schema.data) return
    const { params, fixed } = open.draft
    if (Object.keys(params).length > 0 || Object.keys(fixed).length > 0) return
    const defaults = defaultsFor(schema.data)
    if (Object.keys(defaults.params).length === 0 && Object.keys(defaults.fixed).length === 0) return
    setOpen({ ...open, draft: { ...open.draft, ...defaults } })
  }, [open, schema.data])

  // A version without some of the listing's parameters: they go, or the editor could
  // neither show nor unlock them and publish would refuse the listing.
  useEffect(() => {
    if (!open || !schema.data) return
    const names = new Set((schema.data.parameters ?? []).map((param) => param.name))
    const { params, fixed } = open.draft
    if (Object.keys(params).every((name) => names.has(name)) && Object.keys(fixed).every((name) => names.has(name))) return
    setOpen({
      ...open,
      draft: {
        ...open.draft,
        params: Object.fromEntries(Object.entries(params).filter(([name]) => names.has(name))),
        fixed: Object.fromEntries(Object.entries(fixed).filter(([name]) => names.has(name))),
      },
    })
  }, [open, schema.data])

  const preview = useMemo(
    () => (draft ? previewListing(draft, schema.data, filaments, media, (item) => api.mediaUrl(slug, item)) : null),
    [draft, schema.data, filaments, media, slug],
  )
  const shownValues = useMemo(() => (preview ? { ...startingValues(preview, schema.data), ...values } : {}), [preview, schema.data, values])

  function changed(next: ListingDraft) {
    if (!open) return
    setOpen({ ...open, draft: next })
  }

  /** `run` as `key`'s action: busy while it runs, and its refusal kept as `key`'s. */
  async function act(key: symbol, run: () => Promise<void>) {
    setBusy((cur) => new Set(cur).add(key))
    setError(null)
    try {
      await run()
    } catch (caught) {
      setError({ key, text: message(caught) })
    } finally {
      setBusy((cur) => {
        const next = new Set(cur)
        next.delete(key)
        return next
      })
    }
  }

  /** `next(cur)` applied while `key`'s opening is still the open listing; otherwise the open one is left alone. */
  function whileOpen(key: symbol, next: (cur: Open) => Open | null) {
    setOpen((cur) => (cur?.key === key ? next(cur) : cur))
  }

  /** The open listing saved if it changed; the saved listing, or null when nothing changed. */
  async function save(current: Open): Promise<StorefrontListing | null> {
    const body: StorefrontListingInput = listingInput(current.draft, schema.data, filaments)
    const text = savedKey(body)
    if (current.id !== null && current.saved === text) return null
    const why = unsaveable(body)
    if (why) throw new Error(why)
    const saved =
      current.id === null || current.updatedAt === null
        ? await api.createStorefrontListing(body)
        : await api.updateStorefrontListing(current.id, body, current.updatedAt)
    whileOpen(current.key, (cur) => ({ ...opened(saved, cur.key), draft: cur.draft, saved: text }))
    return saved
  }

  function refresh() {
    setOrder(null)
    listings.reload()
  }

  async function onSave() {
    if (!open) return
    const current = open
    await act(current.key, async () => {
      await save(current)
      refresh()
    })
  }

  async function onPublish() {
    if (!open) return
    const current = open
    await act(current.key, async () => {
      const saved = await save(current)
      const id = saved?.id ?? current.id
      if (id === null) return
      const published = await api.publishStorefrontListing(id)
      // The draft just published, not the row: publish fixes every unnamed parameter at
      // its default, so the row's `fixed` holds more than the draft does.
      const text = savedKey(listingInput(current.draft, schema.data, filaments))
      whileOpen(current.key, (cur) => ({ ...opened(published, cur.key), draft: cur.draft, saved: text }))
      refresh()
    })
  }

  async function onUnpublish() {
    if (!open?.id) return
    const { id, key } = open
    await act(key, async () => {
      const unpublished = await api.unpublishStorefrontListing(id)
      whileOpen(key, (cur) => ({ ...opened(unpublished, key), draft: cur.draft, saved: cur.saved }))
      refresh()
    })
  }

  async function onDelete() {
    if (!open?.id) return
    const { id, key } = open
    await act(key, async () => {
      await api.deleteStorefrontListing(id)
      whileOpen(key, () => null)
      refresh()
    })
  }

  async function reorder(ids: string[]) {
    const byId = new Map(rows.map((row) => [row.id, row]))
    setOrder(ids.flatMap((id) => byId.get(id) ?? []))
    await act(PAGE, async () => {
      try {
        setOrder(await api.orderStorefrontListings(ids))
      } catch (caught) {
        // The server's list again: a refused order (a listing added or removed
        // elsewhere) must not stay on screen, nor make every later move refused too.
        refresh()
        throw caught
      }
    })
  }

  async function setRequestsEnabled(on: boolean) {
    setRequests(on)
    await act(PAGE, async () => {
      try {
        const next = await api.putSettings({ storefront_requests_enabled: on })
        setRequests(next.storefront_requests_enabled)
      } catch (caught) {
        // The stored value again, not the one that was refused.
        setRequests(null)
        throw caught
      }
    })
  }

  const ids = rows.map((row) => row.id)
  /** Whether the open listing's own action runs. */
  const openBusy = open !== null && busy.has(open.key)
  /** Whether any action runs: the order is not moved meanwhile. */
  const anyBusy = busy.size > 0
  const shownError = error && (error.key === PAGE || error.key === open?.key) ? error.text : null
  const requestsOn = requests ?? settings.data?.storefront_requests_enabled ?? false

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
            <Button
              onClick={() => {
                setOpen(fresh())
                setValues(null)
                setError(null)
              }}
            >
              New listing
            </Button>
          </div>
        </div>
        {shownError && (
          <p role="alert" className="text-[13px] text-warn">
            {shownError}
          </p>
        )}
        <div className="grid gap-4 lg:grid-cols-[minmax(0,16rem)_minmax(0,1fr)_minmax(0,20rem)]">
          <section aria-label="Listings" className="flex flex-col gap-2">
            {listings.error && (
              <p role="alert" className="text-[13px] text-warn">
                Could not load the listings: {listings.error.message}
              </p>
            )}
            {listings.data && rows.length === 0 && <p className="text-sm text-muted-foreground">No listings yet. Make one with New listing.</p>}
            <ol className="flex flex-col gap-1">
              {rows.map((row, index) => (
                <li
                  key={row.id}
                  draggable={!anyBusy}
                  onDragStart={() => {
                    if (!anyBusy) setDragging(index)
                  }}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (!anyBusy && dragging !== null && dragging !== index) void reorder(moved(ids, dragging, index))
                    setDragging(null)
                  }}
                  onDragEnd={() => setDragging(null)}
                  className="flex items-center gap-1 rounded-md border bg-card px-1 py-1 data-[open=true]:border-primary"
                  data-open={open?.id === row.id}
                  data-testid="storefront-listing"
                >
                  <GripVerticalIcon aria-hidden="true" className="size-4 shrink-0 cursor-grab text-muted-foreground" />
                  <button
                    type="button"
                    aria-label={`Edit ${row.title}`}
                    className="flex min-w-0 flex-1 flex-col items-start text-left text-sm"
                    onClick={() => {
                      setOpen(opened(row))
                      setValues(null)
                      setError(null)
                    }}
                  >
                    <span className="w-full truncate font-medium">{row.title}</span>
                    {row.published ? <Badge>Published</Badge> : <Badge variant="outline">Not published</Badge>}
                  </button>
                  <Button variant="ghost" size="icon-sm" aria-label={`Move ${row.title} up`} disabled={anyBusy || index === 0} onClick={() => void reorder(moved(ids, index, index - 1))}>
                    <ArrowUpIcon />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Move ${row.title} down`}
                    disabled={anyBusy || index === rows.length - 1}
                    onClick={() => void reorder(moved(ids, index, index + 1))}
                  >
                    <ArrowDownIcon />
                  </Button>
                </li>
              ))}
            </ol>
          </section>

          <section aria-label="Listing" className="flex min-w-0 flex-col gap-3">
            {open && draft ? (
              <>
                <ListingEditor
                  draft={draft}
                  onChange={changed}
                  models={models.data ?? []}
                  versions={versions.data}
                  schema={schema.data}
                  filaments={filaments}
                  media={media}
                />
                <div className="flex flex-wrap gap-2">
                  <Button variant="outline" disabled={openBusy || !draft.title || !draft.commit} onClick={() => void onSave()}>
                    Save
                  </Button>
                  <Button disabled={openBusy || !draft.title || !draft.commit} onClick={() => void onPublish()}>
                    Publish
                  </Button>
                  {open.published && (
                    <Button variant="outline" disabled={openBusy} onClick={() => void onUnpublish()}>
                      Unpublish
                    </Button>
                  )}
                  {open.id !== null && !open.published && (
                    <Button variant="destructive" disabled={openBusy} onClick={() => void onDelete()}>
                      Delete
                    </Button>
                  )}
                </div>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">Choose a listing, or make a new one.</p>
            )}
          </section>

          <section aria-label="Preview" className="flex min-w-0 flex-col gap-2">
            <h2 className="text-sm font-medium">What a customer sees</h2>
            <div data-testid="listing-preview" className="rounded-md border p-3">
              {preview ? (
                <ListingView listing={preview} values={shownValues} onChange={setValues} />
              ) : (
                <p className="text-sm text-muted-foreground">Nothing open.</p>
              )}
            </div>
          </section>
        </div>
      </div>
    </div>
  )
}
