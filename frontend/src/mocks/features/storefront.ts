import { http, HttpResponse } from 'msw'
import type { CustomizerSchema, ListingBound, StorefrontListing, StorefrontListingInput, StorefrontListingPage } from '../../api/types'
import { mockModels, mockSettings, problem } from '../handlers'
import * as orders from './orders/state'
import { filamentOptions, pipelines } from '../fixtures'
import { libraryFiles, pngResponse } from './library'

/**
 * #5282 (spec 2026-11-02 §5.3; backend `api/storefront.py`): the back office's storefront
 * listings. An edit names the `updated_at` it read and unpublishes the listing; a delete
 * is refused while it is published. Publish here checks nothing against the template: the
 * backend's tests do that. It checks only each filament's material (`materialRefusal`, #6258).
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

/** #6237 — the mock's `storefront_prepare_cap`; see `setPrepareCap`. */
let prepareCap = 256
/** #6237 — each prepared listing's combinations made ready so far, by id. */
let prepared = new Map<string, number>()
/** #6299 review — renders the next prepare run cannot start; see `failNextPrepareStarts`. */
let prepareFailures = 0
/** #6299 review — each listing's run's renders that could not be started, by id. */
let notStarted = new Map<string, number>()

export function setPrepareCap(cap: number): void {
  prepareCap = cap
}

/** #6299 review — the next prepare run cannot start `n` of its renders. */
export function failNextPrepareStarts(n: number): void {
  prepareFailures = n
}

const HEX = /^#[0-9a-f]{6}$/i

/**
 * #6237 — the listing's colour combinations: the product of each choice bound whose
 * choices are colours, each colour once whatever its case. The mock's snapshot has no
 * schema, so a colour parameter is told by its choices (the backend reads its type).
 */
function combinationCount(listing: StorefrontListing): number {
  const sizes = Object.values(listing.params)
    .filter((bound) => bound.kind === 'choice' && bound.choices.length > 0 && bound.choices.every((c) => typeof c === 'string' && HEX.test(c)))
    .map((bound) => new Set((bound.kind === 'choice' ? bound.choices : []).map((c) => String(c).toLowerCase())).size)
  return sizes.length === 0 ? 0 : sizes.reduce((a, b) => a * b, 1)
}

/**
 * #6710 PR 6 — the file listings published in this tab, read again from `sessionStorage`:
 * the public page's mock (`storefrontPublic.ts`) shows them, as the CRUD side lists every
 * published listing. The public page is another page load, so it reads them there.
 */
export function publishedFiles(): StorefrontListing[] {
  let stored: StorefrontListing[]
  try {
    stored = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? '[]') as StorefrontListing[]
  } catch {
    stored = []
  }
  return stored.filter((listing) => listing.published && listing.source === 'library_file')
}

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

function refused(status: number, slug: string, detail: string, extensions: Record<string, unknown> = {}) {
  const title = status === 404 ? 'Not Found' : status === 422 ? 'Unprocessable Content' : 'Conflict'
  return problem(status, title, detail, { type: PROBLEMS + slug, ...extensions })
}

/** The backend's 422 `listing_choice_empty` (`orders/storefront/listings.py` `_check_choices`), or null. */
function emptyChoice(body: StorefrontListingInput) {
  const name = Object.entries(body.params ?? {}).find(([, bound]) => bound.kind === 'choice' && bound.choices.length === 0)?.[0]
  if (name === undefined) return null
  return refused(422, 'listing_choice_empty', `'${name}' is offered with nothing to choose; for a colour parameter, choose at least one filament, or lock it`)
}

/** A support material, as the backend's `wire.py` `SUPPORT_MATERIAL` reads it. */
const SUPPORT_MATERIAL = /support|pva|bvoh|hips/i

/**
 * #6258 — the backend's 422 `listing_material_not_allowed` (`orders/storefront/listings.py`
 * `check_materials`), or null: a listing filament of a material neither the listing's
 * `materials` nor, when it names none, the shop's `storefront_materials` allows. The mock
 * does not check the locked colours (`check_locked_colours`): it has no schema. A row a
 * test seeded without `materials` (or one kept in `sessionStorage` from before #6258) has none.
 */
function materialRefusal(listing: StorefrontListing) {
  const own = (listing as Partial<StorefrontListing>).materials ?? []
  const allowed = new Set((own.length > 0 ? own : (mockSettings().storefront_materials ?? [])).map((m) => m.trim().toLowerCase()))
  for (const id of listing.colours) {
    const material = orders.state.filaments.find((f) => f.id === id)?.material
    if (material === undefined || SUPPORT_MATERIAL.test(material) || allowed.has(material.trim().toLowerCase())) continue
    return refused(422, 'listing_material_not_allowed', `'${id}' is ${material}, which this listing does not allow; allow ${material} or take the filament out`, { name: id })
  }
  return null
}

/** #6213 — what a publish copies: one entry per picture, keyed by its media id. */
function publishedMedia(listing: StorefrontListing): StorefrontListing['media'] {
  return listing.media_ids.map((id) => ({ blob_key: `listing-media-${id}`, kind: 'image', media_type: 'image/png', caption: '', media_id: id }))
}

function sorted(): StorefrontListing[] {
  return [...listings].sort((a, b) => a.position - b.position)
}

/**
 * #6290 — each bound as the backend stores and answers it (`model_dump`): an unset `label`
 * and `default`, and a number's unset `step`, are null, never absent.
 */
function storedBounds(params: Record<string, ListingBound>): Record<string, ListingBound> {
  return Object.fromEntries(
    Object.entries(params).map(([name, bound]) => [
      name,
      { ...bound, label: bound.label ?? null, default: bound.default ?? null, ...(bound.kind === 'number' ? { step: bound.step ?? null } : {}) },
    ]),
  )
}

/**
 * #6710 §4.1 — the stored file of a save: the pin (never a body's) kept while the file is the
 * same, else dropped with the request; `repin` written only when the body names it.
 */
function storedFile(body: StorefrontListingInput, found?: StorefrontListing): StorefrontListing['library_file'] {
  const asked = body.library_file
  if (!asked) return null
  const before = found?.library_file
  if (!before || before.id !== asked.id || before.library !== asked.library) {
    return { id: asked.id, library: asked.library, repin: false, pinned: null, filename: null }
  }
  return { ...before, repin: asked.repin ?? before.repin }
}

/** The sha256 the mock's library states for a file: one per id. */
export function mockFileSha(id: number): string {
  return id.toString(16).padStart(64, '0')
}

function fields(body: StorefrontListingInput, found?: StorefrontListing) {
  return {
    source: body.source ?? 'template',
    slug: body.slug ?? null,
    commit: body.commit ?? null,
    library_file: storedFile(body, found),
    title: body.title,
    description: body.description ?? '',
    params: storedBounds(body.params ?? {}),
    fixed: body.fixed ?? {},
    inputs: body.inputs ?? {},
    inputs_v: body.inputs_v ?? null,
    colours: body.colours ?? [],
    media_ids: body.media_ids ?? [],
    materials: body.materials ?? [],
  }
}

/**
 * #5701 — a listing's inputs at its template's version, as a publish stores them, or the
 * refusal: inputs on a template with no pipeline, one the shape requires that is not set, and
 * a number past its limits (the checks of the backend's that these mocks' schemas use).
 */
function pipelineInputs(listing: StorefrontListing): Record<string, unknown> | Response {
  const pipeline = pipelines[listing.slug ?? '']
  const inputs = listing.inputs ?? {}
  if (!pipeline) {
    if (Object.keys(inputs).length === 0) return {}
    return refused(422, 'listing_inputs_unexpected', 'this version of the template has no pipeline, so it takes no inputs', { name: 'inputs' })
  }
  const current = pipeline.declared.inputs_version ?? 0
  const now = (listing.inputs_v ?? current) < current ? { ...pipeline.fresh, ...inputs } : inputs
  const bad = (name: string, reason: string, message: string) => refused(422, 'listing_inputs_invalid', `${name}: ${message}`, { name, reason })
  const shape = pipeline.declared.inputs as { properties: Record<string, { properties: Record<string, { minimum: number; maximum: number }> }> }
  for (const [key, spec] of Object.entries(shape.properties)) {
    const value = now[key] as Record<string, number> | undefined
    if (value === undefined) return bad(`inputs.${key}`, 'required', 'is required')
    for (const [name, { minimum, maximum }] of Object.entries(spec.properties)) {
      const number = value[name]
      if (number === undefined) return bad(`inputs.${key}.${name}`, 'required', 'is required')
      if (number < minimum) return bad(`inputs.${key}.${name}`, 'minimum', `${number} is less than the minimum of ${minimum}`)
      if (number > maximum) return bad(`inputs.${key}.${name}`, 'maximum', `${number} is greater than the maximum of ${maximum}`)
    }
  }
  return now
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

/**
 * `raw` as Pydantic reads an int from a query (measured with `TypeAdapter(int)`, pydantic
 * 2.14.2): decimal digits with an optional sign and surrounding spaces, single `_` between
 * digits, and a fraction of zeros only (`1_000.00` is 1000); `5.`, `.0`, `5.5`, `1e2` and
 * `0x10` are refused (NaN).
 */
function queryInt(raw: string): number {
  return /^\s*[+-]?\d+(_\d+)*(\.0+)?\s*$/.test(raw) ? Number(raw.replace(/_/g, '')) : NaN
}

/** FastAPI's 422 for a `limit` or `offset` out of its bounds, as the real route answers; null when both fit. */
function outOfBounds(url: URL): Response | null {
  const detail = Object.entries(PAGE_BOUNDS).flatMap(([name, [min, max]]) => {
    const raw = url.searchParams.get(name)
    if (raw === null) return []
    const value = queryInt(raw)
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
  const limit = queryInt(get('limit') || '50')
  const offset = queryInt(get('offset') || '0')
  const ordered = sorted()
  const place = new Map(ordered.map((listing, index) => [listing.id, index + 1]))
  const materialOf = new Map(orders.state.filaments.map((f) => [f.id, f.material.toUpperCase()]))
  const matched = ordered.filter(
    (l) =>
      (!q || l.title.toLowerCase().includes(q) || (l.slug ?? '').toLowerCase().includes(q)) &&
      (!status || l.published === (status === 'published')) &&
      (slugs.length === 0 || slugs.includes(l.slug ?? '')) &&
      (materials.length === 0 || l.colours.some((id) => materials.includes(materialOf.get(id) ?? ''))),
  )
  const sort = get('sort') || 'position'
  const dir = get('dir') === 'desc' ? -1 : 1
  const key = (l: StorefrontListing): string | number =>
    sort === 'title' ? l.title.toLowerCase() : sort === 'slug' ? (l.slug ?? '') : sort === 'updated' ? l.updated_at : l.position
  // As the backend's `_SORTS`: a slug sort breaks ties by position, then every sort by id.
  const tie = (a: StorefrontListing, b: StorefrontListing) => (sort === 'slug' ? a.position - b.position : 0)
  const by = [...matched].sort((a, b) => {
    const [x, y] = [key(a), key(b)]
    return (x < y ? -1 : x > y ? 1 : tie(a, b) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)) * dir
  })
  return {
    items: by.slice(offset, offset + limit).map((l) => ({
      id: l.id,
      source: l.source,
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

/** How many pictures were uploaded to file listings: each upload's id is new. */
let uploads = 0

/**
 * #6710 §5 — a file listing's publish: the file read (422 `listing_file_unknown` when the
 * library has none of it), its version pinned and its parts read (the mock's slots are the
 * print dialog's two; a support material is the shop's). With a pin and no request, the
 * pinned version is published again whatever the library holds.
 */
function publishFile(found: StorefrontListing): Response {
  const asked = found.library_file
  const file = asked ? libraryFiles.find((row) => row.id === asked.id) : undefined
  const reads = !asked?.pinned || asked.repin
  if (!asked || (reads && !file)) return refused(422, 'listing_file_unknown', 'the file is not in the library that serves; pick another file', { name: 'library_file' })
  const sha = reads ? mockFileSha(asked.id) : (asked.pinned as string)
  const filename = reads ? (file?.filename ?? '') : (asked.filename ?? '')
  const at = stamp()
  const published: StorefrontListing = {
    ...found,
    library_file: { ...asked, pinned: sha, repin: false, filename },
    published: true,
    // As publish copies a file's pictures (§5.2 step 4): one image per id, its id as `media_id`.
    media: publishedMedia(found),
    published_at: at,
    updated_at: at,
    snapshot: {
      kind: 'library_file',
      file_id: asked.id,
      library: asked.library,
      filename,
      file_type: filename.toLowerCase().endsWith('.stl') ? 'stl' : '3mf',
      sha256: sha,
      size: 64_000,
      copy_key: `listing-file-${sha}`,
      slots: (filamentOptions.slots ?? []).map((slot) => ({
        slot: slot.slot_id - 1,
        colour: slot.colour ?? '#808080',
        material: slot.material ?? null,
        used: true,
        support: /support|pva|bvoh|hips/i.test(slot.material ?? ''),
      })),
      plates: [1],
      preview_keys: [],
      support: {},
      remembered: null,
    },
  }
  listings = listings.map((l) => (l.id === found.id ? published : l))
  save()
  return HttpResponse.json(published)
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
    const edited: StorefrontListing = { ...found, ...fields(body, found), ...UNPUBLISHED, updated_at: stamp() }
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
    // As `_check_pictures`: each id one of the template's images, or a video the listing already shows (#6290).
    const media = mockModels().find((m) => m.slug === found.slug)?.media ?? []
    for (const mediaId of media_ids) {
      const item = media.find((m) => m.id === mediaId && !m.missing)
      if (!item) return refused(422, 'listing_media_unknown', `the template has no media '${mediaId}'`, { name: mediaId })
      if (item.kind !== 'image' && !found.media_ids.includes(mediaId)) return refused(422, 'listing_media_not_image', `media '${mediaId}' is a ${item.kind}, not an image`, { name: mediaId })
    }
    const at = stamp()
    const next: StorefrontListing = { ...found, media_ids, updated_at: at }
    const refusal = found.published ? republishRefusal : null
    republishRefusal = null
    const changed: StorefrontListing = found.published && refusal === null ? { ...next, media: publishedMedia(next), published_at: at } : next
    listings = listings.map((l) => (l.id === id ? changed : l))
    save()
    if (refusal !== null) return refused(422, refusal, `the republish was refused: ${refusal}`, { pictures_saved: true })
    return HttpResponse.json(changed)
  }),

  http.post(`${base}/:id/publish`, ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    const notAllowed = materialRefusal(found)
    if (notAllowed) return notAllowed
    if (found.source === 'library_file') return publishFile(found)
    const pipeline = pipelines[found.slug ?? '']
    const inputs = pipelineInputs(found)
    if (inputs instanceof Response) return inputs
    const at = stamp()
    const published: StorefrontListing = {
      ...found,
      published: true,
      media: publishedMedia(found),
      published_at: at,
      updated_at: at,
      snapshot: {
        kind: 'template',
        schema: EMPTY_SCHEMA,
        pipeline: pipeline !== undefined,
        template_name: found.slug ?? '',
        libraries: [],
        snapshot_key: `src-${found.slug}-${found.commit}`,
        // The backend fills every unnamed parameter's default in here; the mock has no schema.
        fixed: { ...found.fixed },
        inputs,
        inputs_version: pipeline?.declared.inputs_version ?? 0,
      },
    }
    listings = listings.map((l) => (l.id === id ? published : l))
    save()
    return HttpResponse.json(published)
  }),

  // #6710 §5.4 — an upload kept for a file listing, answered as its id.
  http.post(`${base}/:id/pictures`, async ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    if (found.source !== 'library_file') return refused(422, 'listing_source_invalid', 'a template listing takes its pictures from its design', { name: 'media_ids' })
    uploads += 1
    return HttpResponse.json({ id: `upload:${uploads.toString(16).padStart(64, 'f')}` })
  }),

  // #6710 §9.3 — a plate's picture in the pinned copy: 404 with no pin.
  http.get(`${base}/:id/plates/:plate`, ({ params }) => {
    const found = listings.find((l) => l.id === String(params['id']))
    // As `get_listing_plate`: a plain 404, no problem type, for a listing with no pin.
    if (!found?.library_file?.pinned) return HttpResponse.json({ detail: `no picture of plate ${String(params['plate']).replace(/\.png$/, '')}` }, { status: 404 })
    return pngResponse()
  }),

  // #6237 — each read moves a started run on by two combinations (a few at a time, #6299
  // review), until each is ready or could not be started.
  http.get(`${base}/:id/prepare`, ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    if (!found.published) return refused(409, 'listing_not_published', 'Publish this listing before you prepare its colours.')
    const count = combinationCount(found)
    const before = prepared.get(id)
    const failedToStart = notStarted.get(id) ?? 0
    if (count > prepareCap || before === undefined) {
      return HttpResponse.json({ count, cap: prepareCap, ready: 0, rendering: 0, failed: 0, running: false, failed_to_start: failedToStart })
    }
    const ready = Math.min(count - failedToStart, before + 2)
    prepared.set(id, ready)
    const rendering = Math.min(2, count - failedToStart - ready)
    return HttpResponse.json({ count, cap: prepareCap, ready, rendering, failed: 0, running: ready + failedToStart < count, failed_to_start: failedToStart })
  }),

  http.post(`${base}/:id/prepare`, ({ params }) => {
    const id = String(params['id'])
    const found = listings.find((l) => l.id === id)
    if (!found) return refused(404, 'listing_not_found', `no listing '${id}'`)
    if (!found.published) return refused(409, 'listing_not_published', 'Publish this listing before you prepare its colours.')
    const count = combinationCount(found)
    if (count === 0) return refused(422, 'listing_no_colours', 'This listing offers no colour to choose.')
    if (count > prepareCap) {
      return refused(422, 'listing_combinations_over_cap', `This listing has ${count} colour combinations. The shop prepares at most ${prepareCap} at a time.`, { count, cap: prepareCap })
    }
    const before = prepared.get(id)
    const open = before !== undefined && before + (notStarted.get(id) ?? 0) < count
    if (!open) {
      prepared.set(id, before ?? 0)
      notStarted.set(id, Math.min(prepareFailures, count - (before ?? 0)))
      prepareFailures = 0
    }
    return HttpResponse.json({ count, started: !open })
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
  prepareCap = 256
  prepared = new Map()
  prepareFailures = 0
  notStarted = new Map()
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // Nothing stored.
  }
}

load()
