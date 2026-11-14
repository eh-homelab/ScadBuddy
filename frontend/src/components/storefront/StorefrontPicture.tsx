import { useRef, useState } from 'react'
import { Link } from 'react-router'
import { StoreIcon } from 'lucide-react'
import { api, ApiError } from '../../api/client'
import type { MediaView, StorefrontListingRow } from '../../api/types'
import { listingPath } from './listingsQuery'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

/** The most listings one template's dialog reads: the list route's page cap. */
const MAX_LISTINGS = 200

/** What the dialog needs of a listing: the list's row carries it all, as does a whole listing. */
type Row = Pick<StorefrontListingRow, 'id' | 'title' | 'published' | 'media_ids' | 'updated_at'>

function message(caught: unknown): string {
  return caught instanceof ApiError || caught instanceof Error ? caught.message : String(caught)
}

function same(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((id, index) => id === b[index])
}

/**
 * What a refused save did, told from the listing read again after it. `PUT …/media` writes
 * the pictures before it publishes a published listing again, so a refusal may come after
 * the write: the pictures are saved, and the store shows the last publish.
 */
async function afterRefusal(listing: Row, sent: readonly string[], picture: string, caught: unknown): Promise<{ row: Row | null; said: string }> {
  const reason = message(caught)
  let fresh: Row
  try {
    fresh = await api.storefrontListing(listing.id)
  } catch {
    return { row: null, said: `Not confirmed: ${reason}. Open this dialog again to see the listing as it is.` }
  }
  if (same(fresh.media_ids, sent)) {
    return {
      row: fresh,
      said: fresh.published
        ? `Pictures saved, but not published again: ${reason}. The store shows the last published pictures until the listing is published again from its page.`
        : `Pictures saved: ${reason}.`,
    }
  }
  // A picture the listing named before, deleted from the design since, refuses the whole set.
  const stale = caught instanceof ApiError && caught.problem.type?.endsWith('/listing_media_unknown') ? (caught.problem as { name?: unknown }).name : undefined
  if (typeof stale === 'string' && stale !== picture) {
    return { row: fresh, said: `Not saved: the listing still names a picture the design no longer has (${stale}). Remove it in the listing's editor, then try again.` }
  }
  if (caught instanceof ApiError && caught.status === 409) {
    return { row: fresh, said: 'Changed elsewhere since it was read; read again. Save to try again.' }
  }
  return { row: fresh, said: `Not saved: ${reason}` }
}

/**
 * #6213 — "Storefront…" on one image of a template's media: which of the template's
 * listings show it, changed in one place. The listings come from the list route, whose
 * rows carry each one's pictures and `updated_at`. Save sets each changed listing's
 * pictures (`PUT /storefront/listings/{id}/media`): the image is added last, or taken out.
 * A published listing is published again at once and stays on the store; a draft stays a
 * draft. Each listing's answer is shown beside it, and a refused save is told from the
 * listing read again (`afterRefusal`).
 */
export function StorefrontPicture({ slug, item }: { slug: string; item: MediaView }) {
  const [open, setOpen] = useState(false)
  const [listings, setListings] = useState<Row[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [wanted, setWanted] = useState<Record<string, boolean>>({})
  const [results, setResults] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  /** How many listings match in all, when the list holds fewer (`MAX_LISTINGS`). */
  const [more, setMore] = useState<number | null>(null)
  /** Each read's number: only the newest one's answer is applied (the dialog closed and opened again). */
  const reads = useRef(0)
  const name = item.caption || item.file

  async function load() {
    const read = ++reads.current
    setListings(null)
    setLoadError(null)
    setResults({})
    setMore(null)
    try {
      const page = await api.storefrontListings(new URLSearchParams({ slug, limit: String(MAX_LISTINGS) }))
      if (read !== reads.current) return
      setListings(page.items)
      setMore(page.total > page.items.length ? page.total : null)
      setWanted(Object.fromEntries(page.items.map((listing) => [listing.id, listing.media_ids.includes(item.id)])))
    } catch (caught) {
      if (read !== reads.current) return
      setLoadError(`Could not read the listings: ${message(caught)}`)
    }
  }

  const changes = (listings ?? []).filter((listing) => (wanted[listing.id] ?? false) !== listing.media_ids.includes(item.id))

  function replace(row: Row) {
    setListings((all) => (all ?? []).map((one) => (one.id === row.id ? row : one)))
  }

  async function save() {
    setBusy(true)
    const said: Record<string, string> = {}
    for (const listing of changes) {
      const show = wanted[listing.id] ?? false
      const ids = show ? [...listing.media_ids, item.id] : listing.media_ids.filter((id) => id !== item.id)
      try {
        const saved = await api.setStorefrontListingMedia(listing.id, ids, listing.updated_at)
        replace(saved)
        said[listing.id] = show ? (saved.published ? 'Added; live now.' : 'Added to the draft.') : saved.published ? 'Taken off; live now.' : 'Taken off the draft.'
      } catch (caught) {
        const told = await afterRefusal(listing, ids, item.id, caught)
        if (told.row) replace(told.row)
        said[listing.id] = told.said
      }
    }
    setResults(said)
    setBusy(false)
  }

  return (
    <>
      <Button
        size="sm"
        variant="ghost"
        onClick={() => {
          setOpen(true)
          void load()
        }}
      >
        <StoreIcon /> Storefront…
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Show on the storefront</DialogTitle>
            <DialogDescription>
              Choose the listings that show {name ? `“${name}”` : 'this image'}. A published listing is published again at once and stays on the store.
            </DialogDescription>
          </DialogHeader>
          {loadError && (
            <p role="alert" className="text-sm text-destructive">
              {loadError}
            </p>
          )}
          {!listings && !loadError && <p className="text-sm text-muted-foreground">Reading the listings…</p>}
          {listings?.length === 0 && (
            <p className="text-sm text-muted-foreground">
              No listing sells this design yet.{' '}
              <Link to="/storefront" className="underline">
                Open the storefront
              </Link>
              .
            </p>
          )}
          {more !== null && (
            <p className="text-xs text-muted-foreground">
              Showing the first {listings?.length ?? 0} of {more} listings of this design. Change the others on their own pages.
            </p>
          )}
          {listings && listings.length > 0 && (
            <ul aria-label="Listings" className="flex flex-col gap-2">
              {listings.map((listing) => (
                <li key={listing.id} className="flex flex-col gap-0.5">
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={wanted[listing.id] ?? false}
                      aria-label={`Show on ${listing.title}`}
                      disabled={busy}
                      onCheckedChange={(on) => setWanted((all) => ({ ...all, [listing.id]: on === true }))}
                    />
                    <Link to={listingPath(listing.id)} className="min-w-0 truncate hover:underline">
                      {listing.title}
                    </Link>
                    {listing.published ? <Badge>Published</Badge> : <Badge variant="outline">Draft</Badge>}
                  </label>
                  {results[listing.id] && <p className="pl-6 text-xs text-muted-foreground">{results[listing.id]}</p>}
                </li>
              ))}
            </ul>
          )}
          <DialogFooter>
            {listings && listings.length > 1 && (
              <Button variant="ghost" disabled={busy} onClick={() => setWanted(Object.fromEntries(listings.map((listing) => [listing.id, true])))}>
                Choose all
              </Button>
            )}
            <Button disabled={busy || changes.length === 0} onClick={() => void save()}>
              {busy ? 'Saving…' : 'Save'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
