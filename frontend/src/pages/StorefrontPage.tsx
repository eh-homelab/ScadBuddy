import { useEffect, useId, useMemo, useState } from 'react'
import { ArrowDownIcon, ArrowUpIcon, GripVerticalIcon } from 'lucide-react'
import { api, ApiError } from '../api/client'
import type { StorefrontListing, StorefrontListingInput } from '../api/types'
import { ListingEditor } from '../components/storefront/ListingEditor'
import { defaultsFor, draftOf, emptyDraft, listingInput, previewListing, startingValues, type ListingDraft } from '../components/storefront/listingDraft'
import { ListingView, type ParamValues } from '../storefront/components/ListingView'
import { useAsync } from '../lib/useAsync'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'

/** The listing open in the editor: a saved one (its id and the `updated_at` it was read at) or a new one. */
interface Open {
  id: string | null
  updatedAt: string | null
  published: boolean
  draft: ListingDraft
  /** What was last saved, to tell whether the draft needs saving before a publish. */
  saved: string | null
}

function fresh(): Open {
  return { id: null, updatedAt: null, published: false, draft: emptyDraft(), saved: null }
}

function opened(listing: StorefrontListing): Open {
  const draft = draftOf(listing)
  return { id: listing.id, updatedAt: listing.updated_at, published: listing.published, draft, saved: null }
}

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
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
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
    if (!open || !schema.data) return
    const { params, fixed } = open.draft
    if (Object.keys(params).length > 0 || Object.keys(fixed).length > 0) return
    const defaults = defaultsFor(schema.data)
    if (Object.keys(defaults.params).length === 0 && Object.keys(defaults.fixed).length === 0) return
    setOpen({ ...open, draft: { ...open.draft, ...defaults } })
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

  /** The open listing saved if it changed; the saved listing. */
  async function save(current: Open): Promise<StorefrontListing | null> {
    const body: StorefrontListingInput = listingInput(current.draft, schema.data, filaments)
    const text = JSON.stringify(body)
    if (current.id !== null && current.saved === text) return null
    const saved =
      current.id === null || current.updatedAt === null
        ? await api.createStorefrontListing(body)
        : await api.updateStorefrontListing(current.id, body, current.updatedAt)
    setOpen({ ...opened(saved), draft: current.draft, saved: text })
    return saved
  }

  function refresh() {
    setOrder(null)
    listings.reload()
  }

  async function onSave() {
    if (!open) return
    await act(async () => {
      await save(open)
      refresh()
    })
  }

  async function onPublish() {
    if (!open) return
    await act(async () => {
      const saved = await save(open)
      const id = saved?.id ?? open.id
      if (id === null) return
      const published = await api.publishStorefrontListing(id)
      setOpen({ ...opened(published), draft: open.draft, saved: JSON.stringify(listingInput(open.draft, schema.data, filaments)) })
      refresh()
    })
  }

  async function onUnpublish() {
    if (!open?.id) return
    const id = open.id
    await act(async () => {
      const unpublished = await api.unpublishStorefrontListing(id)
      setOpen({ ...opened(unpublished), draft: open.draft, saved: open.saved })
      refresh()
    })
  }

  async function onDelete() {
    if (!open?.id) return
    const id = open.id
    await act(async () => {
      await api.deleteStorefrontListing(id)
      setOpen(null)
      refresh()
    })
  }

  async function reorder(ids: string[]) {
    const byId = new Map(rows.map((row) => [row.id, row]))
    setOrder(ids.flatMap((id) => byId.get(id) ?? []))
    await act(async () => {
      setOrder(await api.orderStorefrontListings(ids))
    })
  }

  async function setRequestsEnabled(on: boolean) {
    setRequests(on)
    await act(async () => {
      const next = await api.putSettings({ storefront_requests_enabled: on })
      setRequests(next.storefront_requests_enabled)
    })
  }

  const ids = rows.map((row) => row.id)
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
        {error && (
          <p role="alert" className="text-[13px] text-warn">
            {error}
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
                  draggable
                  onDragStart={() => setDragging(index)}
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={() => {
                    if (dragging !== null && dragging !== index) void reorder(moved(ids, dragging, index))
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
                  <Button variant="ghost" size="icon-sm" aria-label={`Move ${row.title} up`} disabled={busy || index === 0} onClick={() => void reorder(moved(ids, index, index - 1))}>
                    <ArrowUpIcon />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Move ${row.title} down`}
                    disabled={busy || index === rows.length - 1}
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
                  <Button variant="outline" disabled={busy || !draft.title || !draft.commit} onClick={() => void onSave()}>
                    Save
                  </Button>
                  <Button disabled={busy || !draft.title || !draft.commit} onClick={() => void onPublish()}>
                    Publish
                  </Button>
                  {open.published && (
                    <Button variant="outline" disabled={busy} onClick={() => void onUnpublish()}>
                      Unpublish
                    </Button>
                  )}
                  {open.id !== null && !open.published && (
                    <Button variant="destructive" disabled={busy} onClick={() => void onDelete()}>
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
