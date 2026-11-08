import { http, HttpResponse } from 'msw'
import type { CustomizerSchema, StorefrontListing, StorefrontListingInput } from '../../api/types'
import { problem } from '../handlers'

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

export const handlers = [
  http.get(base, () => HttpResponse.json(sorted())),

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

  http.post(`${base}/:id/publish`, ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    const at = stamp()
    const published: StorefrontListing = {
      ...found,
      published: true,
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
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing stored.
  }
}

load()
