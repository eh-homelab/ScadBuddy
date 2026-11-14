import { useState } from 'react'
import { Link } from 'react-router'
import { StoreIcon } from 'lucide-react'
import { api, ApiError } from '../../api/client'
import type { MediaView, StorefrontListing } from '../../api/types'
import { listingPath } from './listingsQuery'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'

/** The most listings one template's dialog reads: the list route's page cap. */
const MAX_LISTINGS = 200
/** How many listings the dialog reads at once. */
const READS_AT_ONCE = 4

/** Each id read with `read`, at most `READS_AT_ONCE` at a time, in the order given. */
async function readAll(ids: readonly string[], read: (id: string) => Promise<StorefrontListing>): Promise<StorefrontListing[]> {
  const out: StorefrontListing[] = new Array<StorefrontListing>(ids.length)
  let next = 0
  async function worker() {
    while (next < ids.length) {
      const index = next++
      out[index] = await read(ids[index]!)
    }
  }
  await Promise.all(Array.from({ length: Math.min(READS_AT_ONCE, ids.length) }, () => worker()))
  return out
}

function message(caught: unknown): string {
  return caught instanceof ApiError || caught instanceof Error ? caught.message : String(caught)
}

/**
 * #6213 — "Storefront…" on one image of a template's media: which of the template's
 * listings show it, changed in one place. Save sets each changed listing's pictures
 * (`PUT /storefront/listings/{id}/media`): the image is added last, or taken out. A
 * published listing is published again at once and stays on the store; a draft stays a
 * draft. Each listing's answer is shown beside it.
 */
export function StorefrontPicture({ slug, item }: { slug: string; item: MediaView }) {
  const [open, setOpen] = useState(false)
  const [listings, setListings] = useState<StorefrontListing[] | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [wanted, setWanted] = useState<Record<string, boolean>>({})
  const [results, setResults] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const name = item.caption || item.file

  async function load() {
    setListings(null)
    setLoadError(null)
    setResults({})
    try {
      const page = await api.storefrontListings(new URLSearchParams({ slug, limit: String(MAX_LISTINGS) }))
      const read = await readAll(
        page.items.map((row) => row.id),
        (id) => api.storefrontListing(id),
      )
      setListings(read)
      setWanted(Object.fromEntries(read.map((listing) => [listing.id, listing.media_ids.includes(item.id)])))
    } catch (caught) {
      setLoadError(`Could not read the listings: ${message(caught)}`)
    }
  }

  const changes = (listings ?? []).filter((listing) => (wanted[listing.id] ?? false) !== listing.media_ids.includes(item.id))

  async function save() {
    setBusy(true)
    const said: Record<string, string> = {}
    for (const listing of changes) {
      const show = wanted[listing.id] ?? false
      const ids = show ? [...listing.media_ids, item.id] : listing.media_ids.filter((id) => id !== item.id)
      try {
        const saved = await api.setStorefrontListingMedia(listing.id, ids, listing.updated_at)
        setListings((all) => (all ?? []).map((one) => (one.id === saved.id ? saved : one)))
        said[listing.id] = show ? (saved.published ? 'Added; live now.' : 'Added to the draft.') : saved.published ? 'Taken off; live now.' : 'Taken off the draft.'
      } catch (caught) {
        said[listing.id] = `Not saved: ${message(caught)}`
        // Changed elsewhere since it was read: read it again, so Save tries over its newest state.
        if (caught instanceof ApiError && caught.status === 409) {
          try {
            const fresh = await api.storefrontListing(listing.id)
            setListings((all) => (all ?? []).map((one) => (one.id === fresh.id ? fresh : one)))
            said[listing.id] = 'Changed elsewhere since it was read; read again. Save to try again.'
          } catch {
            // Left as it was read; reopening the dialog reads it again.
          }
        }
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
