import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router'
import { ArrowLeftIcon } from 'lucide-react'
import { api, ApiError } from '../api/client'
import type { StorefrontListing, StorefrontListingInput } from '../api/types'
import { ListingEditor } from '../components/storefront/ListingEditor'
import { defaultsFor, draftOf, emptyDraft, listingInput, previewListing, savedKey, sellMedia, storedKey, unsaveable, type ListingDraft } from '../components/storefront/listingDraft'
import { sellListing, type SellListing } from '../components/storefront/sellSource'
import { ListingView, type ParamValues } from '../storefront/components/ListingView'
import type { ParamValue } from '../storefront/api/client'
import { useAsync } from '../lib/useAsync'
import { listingPath } from '../components/storefront/listingsQuery'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

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
  /**
   * #6131 — whether a new listing's parameters, filaments and pictures are set: by "Sell
   * on storefront", or by the smart defaults once its template's schema loads. A template
   * changed in the editor clears it, so the new one's defaults fill in.
   */
  filled: boolean
  /** What "Sell on storefront" said to know before publishing (a version taken instead, a slow render). */
  notes: string[]
}

/** How many listings were opened on this page: each opening's key is named by its number. */
let openings = 0

/** A new opening's key; its description names it uniquely, as the editor's React key. */
function newKey(): symbol {
  openings += 1
  return Symbol(`listing-${openings}`)
}

/** A new listing: blank, or the one "Sell on storefront" filled in. */
function fresh(sold?: SellListing): Open {
  return { key: newKey(), id: null, updatedAt: null, published: false, draft: sold?.draft ?? emptyDraft(), saved: null, filled: sold !== undefined, notes: sold?.notes ?? [] }
}

function opened(listing: StorefrontListing, key: symbol = newKey()): Open {
  const draft = draftOf(listing)
  return { key, id: listing.id, updatedAt: listing.updated_at, published: listing.published, draft, saved: storedKey(listing), filled: true, notes: [] }
}

function message(error: unknown): string {
  return error instanceof ApiError || error instanceof Error ? error.message : String(error)
}

/** What the list's links pass: the list's own query, so "Back to listings" returns to the same page. */
interface BackState {
  back?: string
}

/**
 * `/storefront/listings/<id>` and `/storefront/listings/new` (#5282, spec 2026-11-02 §5.3;
 * #6157) — one listing's editor on its own address, so it can be reloaded, linked and
 * gone back to: its form, Publish, Save draft, Unpublish and Delete, and a live preview of
 * the customer's view, the storefront's own `ListingView` mounted with the unsaved
 * listing. "Back to listings" returns to the list's page, search and filters, which the
 * list passed in the history state; a save stays here.
 *
 * `new?sell=job:<id>` or `new?sell=output:<id>` ("Sell on storefront", #6131) opens the new
 * listing filled in, unsaved; the parameter leaves the address once read.
 */
export function StorefrontListingPage() {
  const { id: routeId = 'new' } = useParams()
  const location = useLocation()
  const navigate = useNavigate()
  const back = (location.state as BackState | null)?.back ?? ''
  const listPath = `/storefront${back}`
  const models = useAsync(() => api.listModels(), [])
  const book = useAsync(() => api.priceBook(), [])
  const [open, setOpen] = useState<Open | null>(() => (routeId === 'new' ? fresh() : null))
  const [loadError, setLoadError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The customer's form starts empty, as on the public page: each field shows the shop's choice.
  const [values, setValues] = useState<ParamValues>({})

  const draft = open?.draft
  const slug = draft?.slug ?? ''
  const commit = draft?.commit ?? ''
  const versions = useAsync(() => (slug ? api.listVersions(slug) : Promise.resolve([])), [slug])
  const schema = useAsync(() => (slug && commit ? api.getSchema(slug, commit) : Promise.resolve(undefined)), [slug, commit])
  const filaments = useMemo(() => book.data?.filaments ?? [], [book.data])
  /** Whether the schema and the price book a saved body is built from are in: until then a saved listing reads as unchanged (#6140 review). */
  const loaded = schema.data !== undefined && (book.data !== undefined || book.error !== undefined)
  const media = useMemo(() => models.data?.find((model) => model.slug === slug)?.media ?? [], [models.data, slug])

  /** The open listing's key now, which an answer that comes late compares against. */
  const openKey = useRef<symbol | null>(open?.key ?? null)
  useEffect(() => {
    openKey.current = open?.key ?? null
  }, [open])
  /** Whether the person changed the open listing since it was opened. */
  const edited = useRef(false)
  /** Whether the open listing holds edits a save has not written: leaving it asks first. */
  const dirty = useRef(false)

  // The address names the listing: read it, unless it is the one already open (a new
  // listing's first save moves the address to its id and keeps its edits).
  useEffect(() => {
    const here = open?.id ?? 'new'
    if (open && here !== routeId && dirty.current) {
      // Unsaved edits are never dropped for a link (#6140 review): the address goes back.
      setError('Save the open listing first, then open the link again.')
      void navigate(open.id ? listingPath(open.id) : '/storefront/listings/new', { replace: true, state: location.state })
      return
    }
    if (routeId === 'new') {
      setOpen((cur) => (cur && cur.id === null ? cur : fresh()))
      setLoadError(null)
      return
    }
    if (openKey.current !== null && open?.id === routeId) return
    let live = true
    setOpen(null)
    setLoadError(null)
    api
      .storefrontListing(routeId)
      .then((listing) => {
        if (!live) return
        edited.current = false
        setOpen(opened(listing))
        setValues({})
      })
      .catch((caught: unknown) => {
        if (!live) return
        setLoadError(caught instanceof ApiError && caught.status === 404 ? 'That listing is gone.' : `Could not read the listing: ${message(caught)}`)
      })
    return () => {
      live = false
    }
    // Only a new address reads again; the open listing is compared as it is then.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeId])

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
  // #6131 — the smart defaults (design 2026-11-13 §4), once the price book has answered.
  useEffect(() => {
    if (!open || open.filled || !schema.data || !(book.data || book.error)) return
    setOpen({ ...open, filled: true, draft: { ...open.draft, ...defaultsFor(schema.data, filaments), media_ids: sellMedia(media) } })
  }, [open, schema.data, book.data, book.error, filaments, media])

  // #6131 — "Sell on storefront" (`?sell=job:<id>` or `?sell=output:<id>`) fills the new
  // listing in. The parameter leaves the address once read, so a reload makes nothing twice.
  const [search, setSearch] = useSearchParams()
  const sell = search.get('sell')
  const [selling, setSelling] = useState(false)
  const sold = useRef<string | null>(null)
  useEffect(() => {
    if (!sell || sold.current === sell || !models.data || !(book.data || book.error)) return
    sold.current = sell
    setSearch(
      (cur) => {
        const next = new URLSearchParams(cur)
        next.delete('sell')
        return next
      },
      { replace: true, state: location.state },
    )
    setSelling(true)
    setError(null)
    // Applied only while the listing open when it started is still the open one: a person
    // who opened another meanwhile keeps it (#6140 review).
    const started = openKey.current
    sellListing(sell, book.data?.filaments ?? [], models.data)
      .then((made) => {
        if (openKey.current !== started) {
          setError('The listing from “Sell on storefront” was not opened, because another listing was opened meanwhile. Choose “Sell on storefront…” again to fill it in.')
          return
        }
        // What the fill-in made is the person's work: leaving it asks first, as an edit does.
        edited.current = true
        setOpen(fresh(made))
        setValues({})
      })
      .catch((caught: unknown) => setError(`Could not fill in the listing: ${message(caught)}`))
      .finally(() => setSelling(false))
  }, [sell, models.data, book.data, book.error, setSearch, location.state])

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

  function changed(next: ListingDraft) {
    if (!open) return
    edited.current = true
    // Another template's defaults fill in once its schema loads.
    setOpen({ ...open, draft: next, filled: open.filled && next.slug === open.draft.slug })
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

  /** `next(cur)` applied while `key`'s opening is still the open listing; otherwise the open one is left alone. */
  function whileOpen(key: symbol, next: (cur: Open) => Open | null) {
    setOpen((cur) => (cur?.key === key ? next(cur) : cur))
  }

  /** A new listing's first save moves the address to its id, keeping the list's query for Back,
   * while that listing is still the open one. */
  function settle(key: symbol, saved: StorefrontListing) {
    if (openKey.current === key && routeId !== saved.id) void navigate(listingPath(saved.id), { replace: true, state: location.state })
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

  async function onSave() {
    if (!open) return
    const current = open
    await act(async () => {
      const saved = await save(current)
      if (saved) settle(current.key, saved)
    })
  }

  async function onPublish() {
    if (!open) return
    const current = open
    await act(async () => {
      const saved = await save(current)
      const id = saved?.id ?? current.id
      if (id === null) return
      const published = await api.publishStorefrontListing(id)
      // The row as published: its `fixed` keeps only the shop's locks (the defaults it
      // filled are the snapshot's), so it is the body that was saved.
      whileOpen(current.key, (cur) => ({ ...opened(published, cur.key), draft: cur.draft, saved: storedKey(published) }))
      settle(current.key, published)
    })
  }

  async function onUnpublish() {
    if (!open?.id) return
    const { id, key } = open
    await act(async () => {
      const unpublished = await api.unpublishStorefrontListing(id)
      whileOpen(key, (cur) => ({ ...opened(unpublished, key), draft: cur.draft, saved: cur.saved }))
    })
  }

  async function onDelete() {
    if (!open?.id) return
    const { id, key } = open
    await act(async () => {
      await api.deleteStorefrontListing(id)
      // Back to the list only while this listing is still the one shown.
      if (openKey.current === key) void navigate(listPath)
    })
  }

  /** #6131 — why Publish and Save draft wait, said beside them. */
  const missing = !draft ? null : !draft.commit ? 'Choose a design and its version to publish.' : !draft.title.trim() ? 'Give the product a title to publish.' : null
  /** #6131 — a published listing with unsaved changes: saving it takes it off the store. */
  const liveChanged = open !== null && open.published && draft !== undefined && loaded && open.saved !== savedKey(listingInput(draft, schema.data, filaments))
  /** Whether the open listing holds what a save would write and has not: a new one always does. */
  const unsaved = open !== null && (open.id === null || (loaded && open.saved !== savedKey(listingInput(open.draft, schema.data, filaments))))
  useEffect(() => {
    dirty.current = edited.current && unsaved
  })
  // Closing the tab or reloading with unsaved edits asks first.
  useEffect(() => {
    function onUnload(event: BeforeUnloadEvent) {
      if (dirty.current) event.preventDefault()
    }
    window.addEventListener('beforeunload', onUnload)
    return () => {
      window.removeEventListener('beforeunload', onUnload)
      // Unmounted: an answer that comes late navigates nowhere.
      openKey.current = null
    }
  }, [])

  return (
    <div className="h-full overflow-y-auto" data-testid="storefront-listing-page">
      <div className="mx-auto flex max-w-6xl flex-col gap-4 px-4 py-6">
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="ghost" size="sm" asChild>
            <Link
              to={listPath}
              onClick={(event) => {
                if (dirty.current && !window.confirm('Leave this listing without saving your changes?')) event.preventDefault()
              }}
            >
              <ArrowLeftIcon />
              Back to listings
            </Link>
          </Button>
          <h1 className="mr-auto min-w-0 truncate text-xl font-semibold tracking-tight">{open?.id === null ? 'New listing' : (open?.draft.title ?? 'Listing')}</h1>
          {open && open.id !== null && (open.published ? <Badge>Published</Badge> : <Badge variant="outline">Draft</Badge>)}
          {unsaved && open?.id !== null && <span className="text-[13px] text-muted-foreground">Unsaved changes</span>}
        </div>
        {(error ?? loadError) && (
          <p role="alert" className="text-[13px] text-warn">
            {error ?? loadError}
          </p>
        )}
        {selling && <p className="text-sm text-muted-foreground">Filling in the listing…</p>}
        {!open && !loadError && (
          <div role="status" aria-label="Loading the listing" className="flex flex-col gap-2">
            {Array.from({ length: 6 }, (_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        )}
        {open && draft && (
          <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
            <section aria-label="Listing" className="flex min-w-0 flex-col gap-3">
              {open.notes.map((note) => (
                <p key={note} role="note" className="rounded-md border border-warn/50 bg-warn/5 p-2 text-[13px]">
                  {note}
                </p>
              ))}
              {liveChanged && <p className="text-[13px] text-muted-foreground">Saving a draft takes this listing off the store until you publish it again.</p>}
              {missing && <p className="text-[13px] text-muted-foreground">{missing}</p>}
              <div className="sticky top-0 z-10 -mx-1 flex flex-wrap gap-2 bg-background px-1 py-2" data-testid="listing-actions">
                <Button disabled={busy || missing !== null} onClick={() => void onPublish()}>
                  Publish
                </Button>
                <Button variant="outline" disabled={busy || missing !== null} onClick={() => void onSave()}>
                  Save draft
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
              <ListingEditor
                key={open.key.description}
                draft={draft}
                onChange={changed}
                models={models.data ?? []}
                versions={versions.data}
                schema={schema.data}
                filaments={filaments}
                media={media}
              />
            </section>

            <section aria-label="Preview" className="flex min-w-0 flex-col gap-2 lg:sticky lg:top-0 lg:max-h-screen lg:self-start lg:overflow-y-auto">
              <h2 className="text-sm font-medium">What a customer sees</h2>
              <div data-testid="listing-preview" className="rounded-md border p-3">
                {preview ? (
                  <ListingView
                    listing={preview}
                    values={values}
                    headingLevel={2}
                    onChange={(name: string, next: ParamValue | undefined) =>
                      setValues(({ [name]: _old, ...rest }) => (next === undefined ? rest : { ...rest, [name]: next }))
                    }
                  />
                ) : (
                  <p className="text-sm text-muted-foreground">Nothing to show yet.</p>
                )}
              </div>
            </section>
          </div>
        )}
      </div>
    </div>
  )
}
