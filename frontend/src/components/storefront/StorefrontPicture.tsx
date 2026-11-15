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
 * What a refused save did. The route says so itself: a refusal after the pictures were
 * written carries `pictures_saved: true` (a published listing whose republish was refused:
 * the store shows the last publish). Any other refusal (`ApiError`) wrote nothing. Only
 * when no answer came does the listing, read again, tell whether the pictures landed. The
 * row is replaced by the listing as it is now whenever it can be read.
 */
async function afterRefusal(listing: Row, sent: readonly string[], picture: string, caught: unknown): Promise<{ row: Row | null; said: string }> {
  const reason = message(caught)
  let fresh: Row | null = null
  try {
    fresh = await api.storefrontListing(listing.id)
  } catch {
    // Left as it was read; the message below says what is known without it.
  }
  if (caught instanceof ApiError) {
    const problem = caught.problem as { type?: string; name?: unknown; pictures_saved?: unknown }
    if (problem.pictures_saved === true) {
      return {
        row: fresh,
        said: `Pictures saved, but not published again: ${reason}. The store shows the last published pictures until the listing is published again from its page.`,
      }
    }
    if (caught.status === 409) {
      return { row: fresh, said: fresh ? 'Not saved: changed elsewhere since it was read. It is read again; Save to try again.' : 'Not saved: changed elsewhere since it was read. Open this dialog again.' }
    }
    // A picture the listing named before and the design no longer has refuses the whole set.
    const named = typeof problem.name === 'string' && problem.name !== picture ? problem.name : null
    if (named && problem.type?.endsWith('/listing_media_unknown')) {
      return { row: fresh, said: `Not saved: the listing still names a picture the design no longer has (${named}). Remove it in the listing's editor, then try again.` }
    }
    return { row: fresh, said: `Not saved: ${reason}` }
  }
  // No answer: only the listing read again tells.
  if (!fresh) return { row: null, said: `Not confirmed: ${reason}. Open this dialog again to see the listing as it is.` }
  if (same(fresh.media_ids, sent)) {
    return { row: fresh, said: `The answer was lost, but the pictures are saved. Open the listing to see whether the store shows them.` }
  }
  return { row: fresh, said: `Not saved: ${reason}` }
}

/**
 * #6213 — "Storefront…" on one image of a template's media: which of the template's
 * listings show it, changed in one place. The listings come from the list route, whose
 * rows carry each one's pictures and `updated_at`. Save sets each changed listing's
 * pictures (`PUT /storefront/listings/{id}/media`): the image is added last, or taken out.
 * A published listing is published again at once and stays on the store; a draft stays a
 * draft. Each listing's answer is shown beside it; a refused save says whether the
 * pictures landed (`afterRefusal`).
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

  /** `row` in place of its listing, while the read `read` is still the one shown. */
  function replace(read: number, row: Row) {
    if (read === reads.current) setListings((all) => (all ?? []).map((one) => (one.id === row.id ? row : one)))
  }

  async function save() {
    // Answers that land after the dialog was opened again (a new read) are not applied to it.
    const read = reads.current
    setBusy(true)
    const said: Record<string, string> = {}
    // Each listing says it is saving as its turn comes: a published one republishes, so a long "Choose all" shows where it is.
    const progress = (id: string, text: string) => {
      said[id] = text
      if (read === reads.current) setResults({ ...said })
    }
    for (const listing of changes) {
      progress(listing.id, wanted[listing.id] ? 'Adding…' : 'Taking off…')
      const show = wanted[listing.id] ?? false
      const ids = show ? [...listing.media_ids, item.id] : listing.media_ids.filter((id) => id !== item.id)
      try {
        const saved = await api.setStorefrontListingMedia(listing.id, ids, listing.updated_at)
        replace(read, saved)
        progress(listing.id, show ? (saved.published ? 'Added; live now.' : 'Added to the draft.') : saved.published ? 'Taken off; live now.' : 'Taken off the draft.')
      } catch (caught) {
        const told = await afterRefusal(listing, ids, item.id, caught)
        if (told.row) replace(read, told.row)
        progress(listing.id, told.said)
      }
    }
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
