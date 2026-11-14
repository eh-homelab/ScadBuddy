import { http, HttpResponse } from 'msw'
import type { CustomizerSchema, StorefrontListing, StorefrontListingInput, StorefrontListingPage } from '../../api/types'
import { mockModels, problem } from '../handlers'
import * as orders from './orders/state'

/**
 * #5282 (spec 2026-11-02 §5.3; backend `api/storefront.py`): the back office's storefront
 * listings. An edit names the `updated_at` it read and unpublishes the listing; a delete
 * is refused while it is published. Publish here checks nothing against the template: the
 * backend's tests do that.
 *
 * The listings are kept in `sessionStorage` too, so a reload shows the order a test set,
 * as the backend's rows would. Each Playwright test has its own context; `reset` clears it.
 */

const base = '/api/v1/storefront/listings'
const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'
const STORAGE_KEY = 'sb-mock-storefront-listings'

let listings: StorefrontListing[] = []
/** #6213 — the problem slug the next republish of a media PUT is refused with, once; see `refuseNextRepublish`. */
let republishRefusal: string | null = null

/**
 * #6213 — the next `PUT …/media` of a published listing writes its pictures, then has its
 * republish refused with 422 `slug`, as the backend's `change_media` does when a publish
 * check fails: the pictures are kept, the last publish stays live.
 */
export function refuseNextRepublish(slug: string): void {
  republishRefusal = slug
}
let ticks = 0

function save(): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(listings))
  } catch {
    // No storage: the listings last as long as the page.
  }
}

function load(): void {
  try {
    listings = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '[]') as StorefrontListing[]
  } catch {
    listings = []
  }
}

/** A later time on every call, so two writes in one millisecond still differ. */
function stamp(): string {
  ticks += 1
  return new Date(Date.now() + ticks).toISOString()
}

function refused(status: number, slug: string, detail: string) {
  const title = status === 404 ? 'Not Found' : status === 422 ? 'Unprocessable Content' : 'Conflict'
  return problem(status, title, detail, { type: PROBLEMS + slug })
}

/** The backend's 422 `listing_choice_empty` (`orders/storefront/listings.py` `_check_choices`), or null. */
function emptyChoice(body: StorefrontListingInput) {
  const name = Object.entries(body.params ?? {}).find(([, bound]) => bound.kind === 'choice' && bound.choices.length === 0)?.[0]
  if (name === undefined) return null
  return refused(422, 'listing_choice_empty', `'${name}' is offered with nothing to choose; for a colour parameter, choose at least one filament, or lock it`)
}

/** #6213 — what a publish copies: one entry per picture, keyed by its media id. */
function publishedMedia(listing: StorefrontListing): StorefrontListing['media'] {
  return listing.media_ids.map((id) => ({ blob_key: `listing-media-${id}`, kind: 'image', media_type: 'image/png', caption: '', media_id: id }))
}

function sorted(): StorefrontListing[] {
  return [...listings].sort((a, b) => a.position - b.position)
}

function fields(body: StorefrontListingInput) {
  return {
    slug: body.slug,
    commit: body.commit,
    title: body.title,
    description: body.description ?? '',
    params: body.params ?? {},
    fixed: body.fixed ?? {},
    colours: body.colours ?? [],
    media_ids: body.media_ids ?? [],
  }
}

const UNPUBLISHED: Pick<StorefrontListing, 'published' | 'snapshot' | 'media' | 'published_at'> = {
  published: false,
  snapshot: null,
  media: [],
  published_at: null,
}

const EMPTY_SCHEMA: CustomizerSchema = { title: null, source_sha256: '', groups: [], parameters: [] }

/** The backend's bounds on a page (`wire.py` `ListingQuery`: `LISTING_PAGE_MAX`, `LISTING_OFFSET_MAX`). */
const PAGE_BOUNDS = { limit: [1, 200], offset: [0, 1_000_000] } as const

/** FastAPI's 422 for a `limit` or `offset` out of its bounds, as the real route answers; null when both fit. */
function outOfBounds(url: URL): Response | null {
  const detail = Object.entries(PAGE_BOUNDS).flatMap(([name, [min, max]]) => {
    const raw = url.searchParams.get(name)
    if (raw === null) return []
    // As Pydantic reads an int from a query (measured with its `TypeAdapter(int)`): decimal
    // digits with an optional sign and surrounding spaces; `1e2` and `0x10` are refused.
    const value = /^\s*[+-]?\d+\s*$/.test(raw) ? Number(raw) : NaN
    if (!Number.isInteger(value)) return [{ type: 'int_parsing', loc: ['query', name], msg: 'Input should be a valid integer' }]
    if (value < min) return [{ type: 'greater_than_equal', loc: ['query', name], msg: `Input should be greater than or equal to ${min}` }]
    if (value > max) return [{ type: 'less_than_equal', loc: ['query', name], msg: `Input should be less than or equal to ${max}` }]
    return []
  })
  return detail.length > 0 ? HttpResponse.json({ detail }, { status: 422 }) : null
}

/** The backend's `ListingStore.page` (#6157): search, filters, sort, a page and the total. */
function page(url: URL): StorefrontListingPage {
  const get = (key: string) => url.searchParams.get(key) ?? ''
  const q = get('q').trim().toLowerCase()
  const slugs = url.searchParams.getAll('slug')
  const materials = url.searchParams.getAll('material').map((m) => m.toUpperCase())
  const status = get('status')
  const limit = Number(get('limit') || 50)
  const offset = Number(get('offset') || 0)
  const ordered = sorted()
  const place = new Map(ordered.map((listing, index) => [listing.id, index + 1]))
  const materialOf = new Map(orders.state.filaments.map((f) => [f.id, f.material.toUpperCase()]))
  const matched = ordered.filter(
    (l) =>
      (!q || l.title.toLowerCase().includes(q) || l.slug.toLowerCase().includes(q)) &&
      (!status || l.published === (status === 'published')) &&
      (slugs.length === 0 || slugs.includes(l.slug)) &&
      (materials.length === 0 || l.colours.some((id) => materials.includes(materialOf.get(id) ?? ''))),
  )
  const sort = get('sort') || 'position'
  const dir = get('dir') === 'desc' ? -1 : 1
  const key = (l: StorefrontListing): string | number =>
    sort === 'title' ? l.title.toLowerCase() : sort === 'slug' ? l.slug : sort === 'updated' ? l.updated_at : l.position
  // As the backend's `_SORTS`: a slug sort breaks ties by position, then every sort by id.
  const tie = (a: StorefrontListing, b: StorefrontListing) => (sort === 'slug' ? a.position - b.position : 0)
  const by = [...matched].sort((a, b) => {
    const [x, y] = [key(a), key(b)]
    return (x < y ? -1 : x > y ? 1 : tie(a, b) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)) * dir
  })
  return {
    items: by.slice(offset, offset + limit).map((l) => ({
      id: l.id,
      slug: l.slug,
      commit: l.commit,
      title: l.title,
      place: place.get(l.id) ?? 0,
      published: l.published,
      media_id: l.media_ids[0] ?? null,
      media_ids: l.media_ids,
      published_at: l.published_at,
      updated_at: l.updated_at,
    })),
    total: matched.length,
    places: ordered.length,
    limit,
    offset,
  }
}

export const handlers = [
  http.get(base, ({ request }) => {
    const url = new URL(request.url)
    return outOfBounds(url) ?? HttpResponse.json(page(url))
  }),

  http.get(`${base}/:id`, ({ params }) => {
    const found = listings.find((l) => l.id === String(params['id']))
    return found ? HttpResponse.json(found) : refused(404, 'listing_not_found', `no listing '${String(params['id'])}'`)
  }),

  http.post(`${base}/:id/move`, async ({ params, request }) => {
    const id = String(params['id'])
    const { place } = (await request.json()) as { place: number }
    const ids = sorted().map((l) => l.id)
    if (!ids.includes(id)) return refused(404, 'listing_not_found', `no listing '${id}'`)
    const rest = ids.filter((other) => other !== id)
    rest.splice(Math.min(place, rest.length + 1) - 1, 0, id)
    listings = listings.map((l) => ({ ...l, position: rest.indexOf(l.id) }))
    save()
    return HttpResponse.json(listings.find((l) => l.id === id))
  }),

  http.post(base, async ({ request }) => {
    const body = (await request.json()) as StorefrontListingInput
    const empty = emptyChoice(body)
    if (empty) return empty
    const at = stamp()
    const listing: StorefrontListing = {
      ...fields(body),
      ...UNPUBLISHED,
      id: crypto.randomUUID(),
      position: listings.reduce((last, l) => Math.max(last, l.position + 1), 0),
      created_at: at,
      updated_at: at,
    }
    listings.push(listing)
    save()
    return HttpResponse.json(listing, { status: 201 })
  }),

  http.put(`${base}/order`, async ({ request }) => {
    const { ids } = (await request.json()) as { ids: string[] }
    const known = new Set(listings.map((l) => l.id))
    if (ids.length !== known.size || new Set(ids).size !== ids.length || !ids.every((id) => known.has(id))) {
      return refused(409, 'listing_changed', 'the order must name every listing once; load the listings again')
    }
    listings = listings.map((l) => ({ ...l, position: ids.indexOf(l.id) }))
    save()
    return HttpResponse.json(sorted())
  }),

  http.put(`${base}/:id`, async ({ params, request }) => {
    const id = String(params['id'])
    const { listing: body, updated_at } = (await request.json()) as { listing: StorefrontListingInput; updated_at: string }
    const empty = emptyChoice(body)
    if (empty) return empty
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    if (found.updated_at !== updated_at) return refused(409, 'listing_changed', `listing '${id}' changed since it was read`)
    const edited: StorefrontListing = { ...found, ...fields(body), ...UNPUBLISHED, updated_at: stamp() }
    listings = listings.map((l) => (l.id === id ? edited : l))
    save()
    return HttpResponse.json(edited)
  }),

  // #6213 — the pictures alone; a published listing is published again with them.
  http.put(`${base}/:id/media`, async ({ params, request }) => {
    const id = String(params['id'])
    const { media_ids, updated_at } = (await request.json()) as { media_ids: string[]; updated_at: string }
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    if (found.updated_at !== updated_at) return refused(409, 'listing_changed', `listing '${id}' changed since it was read`)
    if (new Set(media_ids).size !== media_ids.length) return refused(422, 'listing_media_duplicate', 'a picture is named twice')
    // As `_check_pictures`: each id one of the template's images.
    const media = mockModels().find((m) => m.slug === found.slug)?.media ?? []
    for (const mediaId of media_ids) {
      const item = media.find((m) => m.id === mediaId && !m.missing)
      if (!item) return refused(422, 'listing_media_unknown', `the template has no media '${mediaId}'`)
      if (item.kind !== 'image') return refused(422, 'listing_media_not_image', `media '${mediaId}' is a ${item.kind}, not an image`)
    }
    const at = stamp()
    const next: StorefrontListing = { ...found, media_ids, updated_at: at }
    const refusal = found.published ? republishRefusal : null
    republishRefusal = null
    const changed: StorefrontListing = found.published && refusal === null ? { ...next, media: publishedMedia(next), published_at: at } : next
    listings = listings.map((l) => (l.id === id ? changed : l))
    save()
    if (refusal !== null) return refused(422, refusal, `the republish was refused: ${refusal}`)
    return HttpResponse.json(changed)
  }),

  http.post(`${base}/:id/publish`, ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    const at = stamp()
    const published: StorefrontListing = {
      ...found,
      published: true,
      media: publishedMedia(found),
      published_at: at,
      updated_at: at,
      snapshot: {
        schema: EMPTY_SCHEMA,
        pipeline: false,
        template_name: found.slug,
        libraries: [],
        snapshot_key: `src-${found.slug}-${found.commit}`,
        // The backend fills every unnamed parameter's default in here; the mock has no schema.
        fixed: { ...found.fixed },
      },
    }
    listings = listings.map((l) => (l.id === id ? published : l))
    save()
    return HttpResponse.json(published)
  }),

  http.post(`${base}/:id/unpublish`, ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    const unpublished: StorefrontListing = { ...found, ...UNPUBLISHED, updated_at: stamp() }
    listings = listings.map((l) => (l.id === id ? unpublished : l))
    save()
    return HttpResponse.json(unpublished)
  }),

  http.delete(`${base}/:id`, ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    if (found.published) return refused(409, 'listing_published', `listing '${id}' is published; unpublish it first`)
    listings = listings.filter((l) => l.id !== id)
    save()
    return new HttpResponse(null, { status: 204 })
  }),
]

export function reset(): void {
  listings = []
  republishRefusal = null
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing stored.
  }
}

load()
