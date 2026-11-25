import { http, HttpResponse } from 'msw'
import type { CustomerOrder, Draft, Listing, ListingCard, ParamValue, StoreEstimate, StoreRender, StoreVariant } from '../../storefront/api/client'
import type { StorefrontListing } from '../../api/types'
import { closestColour } from '../../storefront/closestColour'
import { colourPlan } from '../../storefront/colours'
import { customerFilaments } from '../../storefront/params'
import { buildGlb, keychainGlb } from '../glb'
import { problem } from '../handlers'
import * as orderState from './orders/state'
import { publishedFiles } from './storefront'
import { advanceChecks, modelsOrderOf } from './storefrontModels'

/**
 * #5282 (spec 2026-11-02 §5.1-§5.4; backend `storefront/routes.py`): the public page's edge,
 * `/api/store/v1`. Two listings: a keychain that offers a name, a size and a colour, and a
 * tag whose one colour choice lost every filament (all archived), so it cannot be ordered.
 * A render is pending once, then done, with an estimate of $12.40. A write without an
 * `Idempotency-Key` is refused, as the edge refuses it (428); a value the listing does not
 * offer is refused as the CRUD side refuses it (422).
 *
 * The draft is kept in `sessionStorage` too, so a reload or a new page shows the cart, as
 * the edge's cookie would; `reset` clears it.
 *
 * The customer's order page (#5283): the token `tok` (`/o#tok`) opens one order whose
 * design waits for an answer at $12.50. Accepting it approves the item and opens a
 * payment request with a pay link, as the CRUD side's last accept does; any other token
 * is a 404 with no body. A cancel is refused 409 `order_approved` once anything is approved
 * and 409 `order_paid` once a payment is paid, as the CRUD side's.
 */

const base = '/api/store/v1'
const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'
/** The shop's default materials (`storefront_materials`), in its order (#6258). */
const SHOP_MATERIALS = ['PETG', 'PLA']
const STORAGE_KEY = 'sb-mock-storefront-draft'

export const KEYCHAIN_ID = 'l1'
export const TAG_ID = 'l2'
const BLUE = { id: 'f-blue', colour: '#1B6CA8', colour_name: 'Ocean blue', material: 'PLA', alpha: 255, extra_colours: [], effect: null }
const ORANGE = { id: 'f-orange', colour: '#E8532F', colour_name: 'Signal orange', material: 'PLA', alpha: 255, extra_colours: [], effect: null }

export const KEYCHAIN: Listing = {
  id: KEYCHAIN_ID,
  source: 'template',
  title: 'Name keychain',
  description: 'A keychain with your name raised on it, in two colours.',
  params: {
    name: { kind: 'text', label: 'Name', group: '', colour: false, max_length: 20 },
    size: { kind: 'number', label: 'Length (mm)', group: '', colour: false, min: 40, max: 80, step: 5, default: 60 },
    base_colour: { kind: 'choice', label: 'Base colour', group: '', colour: true, choices: ['#1B6CA8', '#E8532F'], default: '#1b6ca8' },
  },
  colours: [BLUE, ORANGE],
  media: [],
  materials: SHOP_MATERIALS,
  updated_at: '2026-11-07T12:00:00Z',
  available: true,
}

export const TAG: Listing = {
  id: TAG_ID,
  source: 'template',
  title: 'Luggage tag',
  description: 'A tag for a bag, in one colour.',
  params: {
    colour: { kind: 'choice', label: 'Colour', group: '', colour: true, choices: [] },
  },
  colours: [],
  media: [],
  materials: SHOP_MATERIALS,
  updated_at: '2026-11-07T12:00:00Z',
  available: false,
}

export const CHARM_ID = 'l3'
const PETG_TEAL = { id: 'f-petg-teal', colour: '#1C8C8C', colour_name: 'Teal', material: 'PETG' }
const PETG_RUST = { id: 'f-petg-rust', colour: '#C0502A', colour_name: 'Rust', material: 'PETG' }

/**
 * #6152, #6258: a listing in two materials. A print is of one: the first of the shop's
 * order (PETG, then PLA) that has every colour the visitor sets.
 */
export const CHARM: Listing = {
  id: CHARM_ID,
  source: 'template',
  title: 'Bag charm',
  description: 'A charm in two colours of one material.',
  params: {
    body_colour: {
      kind: 'choice',
      label: 'Body colour',
      group: '',
      colour: true,
      choices: [BLUE.colour, ORANGE.colour, PETG_TEAL.colour, PETG_RUST.colour],
      default: BLUE.colour,
    },
    rim_colour: {
      kind: 'choice',
      label: 'Rim colour',
      group: '',
      colour: true,
      choices: [BLUE.colour, ORANGE.colour, PETG_TEAL.colour, PETG_RUST.colour],
      default: BLUE.colour,
    },
  },
  colours: [BLUE, ORANGE, PETG_TEAL, PETG_RUST],
  media: [],
  materials: SHOP_MATERIALS,
  updated_at: '2026-11-07T12:00:00Z',
  available: true,
}

export const FILE_ID = 'l4'

/**
 * #6710: a listing of a library file, in two parts and two plates. Its colours are PLA
 * first: each part starts at the PLA colour closest to the file's own (`slots`' defaults).
 * Its price is the file price below, once worked out; a preview is the held GLB of a plate,
 * each mesh with its slot.
 */
export const FILE: Listing = {
  id: FILE_ID,
  source: 'library_file',
  title: 'Bag clip',
  description: 'A clip for a bag strap, printed in two parts.',
  params: {},
  colours: [BLUE, ORANGE, PETG_TEAL, PETG_RUST],
  media: [],
  materials: ['PLA', 'PETG'],
  updated_at: '2026-11-07T12:00:00Z',
  available: true,
  slots: [
    { slot: 0, label: 'Part 1', default: BLUE.id },
    { slot: 1, label: 'Part 2', default: ORANGE.id },
  ],
  previews: [`${base}/listings/${FILE_ID}/preview/1.glb`, `${base}/listings/${FILE_ID}/preview/2.glb`],
}

const LISTINGS = [KEYCHAIN, TAG, CHARM, FILE]

/**
 * The listings the public page shows: the fixed ones, then each file listing the back
 * office's mock published in this tab (`storefront.ts`, kept in `sessionStorage`), as the
 * CRUD side's `public_listing` shows it (#6710 PR 6).
 */
function allListings(): Listing[] {
  return [...LISTINGS, ...publishedFiles().flatMap((listing) => publicFile(listing))]
}

/** A back-office file listing as the public page reads it, or nothing when it offers no colour. */
function publicFile(listing: StorefrontListing): Listing[] {
  const snapshot = listing.snapshot
  if (!snapshot || snapshot.kind !== 'library_file') return []
  const book = orderState.state.filaments.filter((f) => listing.colours.includes(f.id) && !f.archived_at)
  const colours = customerFilaments(book).map((f) => ({
    id: f.id,
    colour: f.colour.slice(0, 7),
    colour_name: f.colour_name ?? null,
    material: f.material,
    alpha: f.alpha ?? 255,
    extra_colours: [],
    effect: null,
  }))
  const materials = listing.materials.length > 0 ? listing.materials : SHOP_MATERIALS
  const first = materials.map((m) => colours.filter((f) => f.material.toLowerCase() === m.toLowerCase())).find((each) => each.length > 0) ?? []
  const offered = snapshot.slots.filter((slot) => slot.used && !slot.support)
  return [
    {
      id: listing.id,
      source: 'library_file',
      title: listing.title,
      description: listing.description,
      params: {},
      colours,
      media: [],
      materials,
      updated_at: listing.updated_at,
      available: first.length > 0,
      slots: offered.map((slot, index) => {
        const at = closestColour({ hex: slot.colour, name: null }, first.map((f) => ({ hex: f.colour, name: f.colour_name, alpha: f.alpha })))
        return { slot: slot.slot, label: `Part ${index + 1}`, default: at === null ? null : first[at]!.id }
      }),
      previews: snapshot.plates.map((plate) => `${base}/listings/${listing.id}/preview/${plate}.glb`),
    },
  ]
}

/** A listing as the public list shows it (#6154): its first picture, and whether it can be ordered. */
export function card(listing: Listing): ListingCard {
  const image = listing.media.find((media) => media.kind === 'image') ?? null
  const available = !Object.values(listing.params).some((param) => param.kind === 'choice' && param.colour && param.choices.length === 0)
  return { id: listing.id, title: listing.title, description: listing.description, image, available }
}

/** One page of `listings`, as the edge pages them: the cursor is the next card's index. */
export function listingsPage(listings: readonly Listing[], url: URL, requestsEnabled = true) {
  const q = url.searchParams.get('q')?.trim().toLowerCase() ?? ''
  const limit = Number(url.searchParams.get('limit') ?? 24)
  const start = Number(url.searchParams.get('cursor') ?? 0)
  const matched = listings.filter((listing) => listing.title.toLowerCase().includes(q))
  const page = matched.slice(start, start + limit)
  return {
    listings: page.map(card),
    next_cursor: start + limit < matched.length ? String(start + limit) : null,
    requests_enabled: requestsEnabled,
  }
}

/** A slice price (#6153): firm, and only the price. */
export const ESTIMATE: StoreEstimate = {
  source: 'slice',
  price_minor: 1240,
  currency: 'USD',
  minor_exponent: 2,
  retry_after: null,
}
/** A file listing's slice price (#6710 §7.5): firm once its slice is done. */
export const FILE_ESTIMATE: StoreEstimate = { ...ESTIMATE, price_minor: 700 }
/** The price while the shop's slice runs: read again after `retry_after` seconds. */
export const PENDING_PRICE: StoreEstimate = { source: 'pending', price_minor: null, currency: null, minor_exponent: null, retry_after: 2 }
/** No firm price: the shop prices the design by hand. */
export const NO_PRICE: StoreEstimate = { source: 'none', price_minor: null, currency: null, minor_exponent: null, retry_after: null }

/** The shop's shipping options (backend `storefront_shipping_options`): one ships, one does not. */
const SHIPPING = [
  { id: 'post', label: 'Post', amount_minor: 700, ships: true },
  { id: 'pickup', label: 'Pick up at the workshop', amount_minor: 0, ships: false },
]
let orders = 0

let draft: Draft | null = null
let renders = new Map<string, { render: StoreRender; polls: number; variant: string; estimates: number }>()
let serial = 0
/**
 * #6921: what the shop holds of each variant (the listing and the values): the price and
 * preview its render's estimate priced. The instant read answers it, with no render.
 */
let variants = new Map<string, StoreVariant>()
/** #6710: how often each file listing's choice of colours was priced. */
let fileReads = new Map<string, number>()
/** #6710 §7.5: the file listings that have a price of any colours: their `last` price. */
let filePriced = new Set<string>()

/** A file listing's plate (#6710 §8.2): a base and a clip, each mesh with its slot. */
function fileGlb(): Uint8Array {
  return buildGlb([
    { color: '#FF6AC1', slot: 0, min: [-25, 0, -8], max: [25, 3, 8] },
    { color: '#1F6FEB', slot: 1, min: [-10, 3, -4], max: [10, 9, 4] },
  ])
}

/** The mock's variant key: the listing and the values, as the CRUD side's `variant_key` keys them. */
export function variantOf(body: { listing_id?: string; listing_updated_at?: string; values?: Record<string, unknown> }): string {
  const values = Object.fromEntries(Object.entries(body.values ?? {}).sort(([a], [b]) => a.localeCompare(b)))
  return JSON.stringify([body.listing_id, body.listing_updated_at, values])
}

/** A preview of the keychain, as a priced variant's output gives it. */
export function cachedPreview(key: string): StoreVariant['preview'] {
  return {
    url: `${base}/variants/${key}/preview.glb`,
    colors: ['#1B6CA8', '#E8532F'],
    bbox_mm: { min: [-30, -10, 0], max: [30, 10, 6], size: [60, 20, 6] },
    plates: [],
  }
}

/** Holds a variant as priced before (#6921): a test seeds the instant read with it. */
export function holdVariant(body: Parameters<typeof variantOf>[0], held: Omit<StoreVariant, 'values'>): void {
  variants.set(variantOf(body), { values: null, ...held })
}

/** The visitor's choices as one hex key: what the edge's preview path takes. */
function hexKey(variant: string): string {
  let hash = 0
  for (const char of variant) hash = (hash * 31 + char.charCodeAt(0)) >>> 0
  return hash.toString(16).padStart(8, '0').repeat(8)
}

export function save(): void {
  try {
    sessionStorage.setItem(STORAGE_KEY, JSON.stringify(draft))
  } catch {
    // No storage: the draft lasts as long as the page.
  }
}

function load(): void {
  try {
    draft = JSON.parse(sessionStorage.getItem(STORAGE_KEY) ?? 'null') as Draft | null
  } catch {
    draft = null
  }
}

export function next(): string {
  serial += 1
  return serial.toString(16).padStart(32, '0')
}

export function refused(status: number, slug: string, detail: string) {
  return problem(status, status === 404 ? 'Not Found' : 'Unprocessable Content', detail, { type: PROBLEMS + slug })
}

export function keyMissing(request: Request) {
  return request.headers.get('Idempotency-Key')
    ? null
    : refused(428, 'idempotency_key_required', 'Send an Idempotency-Key with this request.')
}

interface Body {
  listing_id?: string
  listing_updated_at?: string
  values?: Record<string, unknown>
  colours?: Record<string, string>
  quantity?: number
}

/**
 * #6710: a file listing's colours as the CRUD side checks them (`file_prices.py`
 * `file_choice`): no values, a listing filament for each offered slot and no other, and
 * one material, the first slot's (422 `listing_colours_clash` naming the others).
 */
function fileOutside(listing: Listing, body: Pick<Body, 'values' | 'colours'>): Response | null {
  const colours = body.colours ?? {}
  const slots = (listing.slots ?? []).map((part) => String(part.slot))
  const outsideIt = () => refused(422, 'input_outside_listing', 'This choice is not offered for this listing.')
  if (Object.keys(body.values ?? {}).length > 0) return outsideIt()
  if (Object.keys(colours).some((slot) => !slots.includes(slot)) || slots.some((slot) => !(slot in colours))) return outsideIt()
  const picked = slots.map((slot) => [slot, listing.colours.find((f) => f.id === colours[slot])] as const)
  if (picked.some(([, filament]) => !filament)) return outsideIt()
  const material = picked[0]?.[1]?.material.toLowerCase()
  const clashing = picked.filter(([, filament]) => filament!.material.toLowerCase() !== material).map(([slot]) => `colours.${slot}`)
  if (clashing.length > 0) {
    return problem(422, 'Unprocessable Content', "Some of these colours can't go together: each print is made from one kind of plastic. Change one of them.", {
      type: PROBLEMS + 'listing_colours_clash',
      parts: clashing,
    })
  }
  return null
}

/** 409 `listing_changed`: the listing moved, or was switched, since its page read it. */
function changed(): Response {
  return problem(409, 'Conflict', 'This listing has changed. Load it again.', { type: PROBLEMS + 'listing_changed' })
}

/**
 * The CRUD side's checks (`orders/storefront/inputs.py`), as far as the mocks need them. A
 * render and its instant read take a template listing only (`files` false): a file listing
 * is 409 `listing_changed` there, as the CRUD side's `published_listing` answers (#6710).
 */
function outside(body: Body, files = true): Response | Listing {
  const listing = allListings().find((l) => l.id === body.listing_id)
  if (!listing) return refused(404, 'listing_not_found', 'There is no such listing.')
  if (!files && listing.source === 'library_file') return changed()
  if (listing.source === 'library_file') return fileOutside(listing, body) ?? listing
  for (const [name, value] of Object.entries(body.values ?? {})) {
    const bound = listing.params[name]
    if (!bound) return refused(422, 'input_outside_listing', 'This choice is not offered for this listing.')
    if (bound.kind === 'choice' && !bound.choices.includes(value as string)) {
      return refused(422, 'input_outside_listing', 'This choice is not offered for this listing.')
    }
  }
  // #6258: the colours given fix one material, the first of the listing's that has each
  // (`inputs.py` `resolve_choice`); none is 422 `listing_colours_clash` with the parts.
  const given = Object.entries(body.values ?? {}).filter(([name]) => {
    const bound = listing.params[name]
    return bound?.kind === 'choice' && bound.colour
  })
  const offers = (material: string, colour: unknown) =>
    listing.colours.some((f) => f.material.toLowerCase() === material.toLowerCase() && f.colour.toLowerCase() === String(colour).toLowerCase())
  if (given.length > 0 && !listing.materials.some((material) => given.every(([, colour]) => offers(material, colour)))) {
    return problem(422, 'Unprocessable Content', "Some of these colours can't go together: each print is made from one kind of plastic. Change one of them.", {
      type: PROBLEMS + 'listing_colours_clash',
      parts: given.map(([name]) => name),
    })
  }
  const archived = Object.values(listing.params).some((p) => p.kind === 'choice' && p.colour && p.choices.length === 0)
  if (archived) return refused(422, 'input_outside_listing', 'This choice is not offered for this listing.')
  return listing
}

export const ORDER_TOKEN = 'tok'
/** The pay page's token (`/pay#pay`). */
export const PAY_TOKEN = 'pay'
/** The order a signed-in reader opens at `/o?order=<id>`. */
export const ORDER_ID = 'order-1'

function freshOrder(): CustomerOrder {
  return {
    number: 'ORD-2026-7',
    status: 'We are reviewing your order',
    currency: 'USD',
    minor_exponent: 2,
    can_cancel: true,
    items: [
      {
        id: 'oi1',
        title: 'Name keychain',
        quantity: 1,
        status: 'Price ready',
        revisions: [
          {
            id: 'r1',
            number: 1,
            note: 'Here is your keychain with "Ada" raised on it.',
            sent_at: '2026-11-09T10:00:00Z',
            answer: null,
            answer_note: null,
            can_answer: true,
            pictures: [],
            colours: [{ slot: 0, colour: '#1B6CA8', name: 'Ocean blue' }],
          },
        ],
        quote: { id: 'q1', unit_price_minor: 1250, quantity: 1, total_minor: 1250, accepted: false, can_accept: false },
      },
    ],
    payments: [],
    payment_instructions: null,
    shipments: [],
  }
}

let order = freshOrder()

/** The order the body's token, or a signed-in reader's `order_id`, opens, or the empty 404 the CRUD side answers. */
async function orderOf(request: Request): Promise<CustomerOrder | Response> {
  const body = (await request.json().catch(() => ({}))) as { token?: unknown; order_id?: unknown }
  return body.token === ORDER_TOKEN || (body.token === undefined && body.order_id === ORDER_ID)
    ? order
    : new HttpResponse(null, { status: 404 })
}

export function ensureDraft(): Draft {
  draft ??= { id: 'draft-1', currency: 'USD', minor_exponent: 2, items: [], shipping_options: SHIPPING, pay_now: false }
  return draft
}

export const handlers = [
  http.post(`${base}/pay/checkout`, async ({ request }) => {
    const body = (await request.json().catch(() => ({}))) as { token?: unknown }
    return body.token === PAY_TOKEN
      ? HttpResponse.json({
          amount_minor: 1250,
          currency: 'USD',
          order_number: 'ORD-2026-7',
          client_id: 'sb',
          paypal_order_id: 'MOCK-ORDER',
          minor_exponent: 2,
        })
      : new HttpResponse(null, { status: 404 })
  }),
  http.get(`${base}/listings`, ({ request }) => HttpResponse.json(listingsPage(allListings(), new URL(request.url)))),
  http.get(`${base}/listings/:id`, ({ params }) => {
    const listing = allListings().find((l) => l.id === params.id)
    return listing ? HttpResponse.json(listing) : refused(404, 'listing_not_found', 'There is no such listing.')
  }),
  // #6710: a file listing's held preview of a plate, each mesh with its slot.
  http.get(`${base}/listings/:id/preview/:file`, ({ params }) => {
    const listing = allListings().find((l) => l.id === params.id)
    const path = `${base}/listings/${String(params.id)}/preview/${String(params.file)}`
    if (!listing?.previews?.includes(path)) return refused(404, 'preview_not_found', 'This design has no such preview.')
    return HttpResponse.arrayBuffer(fileGlb().slice().buffer as ArrayBuffer, { headers: { 'Content-Type': 'model/gltf-binary' } })
  }),
  // #6710 §7.5: a file listing's price. The first read of a choice of colours starts its
  // slice and answers the listing's `last` price (`pending` when it has none), the next
  // answers the price, as the shop's slice price does.
  http.post(`${base}/listings/:id/price`, async ({ request, params }) => {
    const listing = allListings().find((l) => l.id === params.id)
    if (!listing) return refused(404, 'listing_not_found', 'There is no such listing.')
    const body = (await request.json()) as Pick<Body, 'listing_updated_at' | 'colours'>
    if (listing.source !== 'library_file' || body.listing_updated_at !== listing.updated_at) return changed()
    const checked = fileOutside(listing, body)
    if (checked) return checked
    const key = JSON.stringify([listing.id, body.colours])
    const reads = (fileReads.get(key) ?? 0) + 1
    fileReads.set(key, reads)
    if (reads > 1) {
      filePriced.add(listing.id)
      return HttpResponse.json(FILE_ESTIMATE)
    }
    return HttpResponse.json(filePriced.has(listing.id) ? { ...FILE_ESTIMATE, source: 'last', retry_after: 0 } : { ...PENDING_PRICE, retry_after: 0 })
  }),
  http.post(`${base}/renders`, async ({ request }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const body = (await request.json()) as Body
    const checked = outside(body, false)
    if (checked instanceof Response) return checked
    const id = next()
    const render: StoreRender = {
      id,
      status: 'pending',
      stage: null,
      plates: [],
      bbox_mm: null,
      colors: null,
      preview_url: null,
      error: null,
      // #6258, #6302 review: the colours the render takes. A part not given moves to the
      // closest colour of the material, as the shop's `resolve_choice` does: the mock uses
      // the page's copy of that rule.
      values: colourPlan(checked, (body.values ?? {}) as Record<string, ParamValue>).shown,
    }
    renders.set(id, { render, polls: 0, variant: variantOf(body), estimates: 0 })
    return HttpResponse.json(render, { status: 202 })
  }),
  http.get(`${base}/renders/:id`, ({ params }) => {
    const held = renders.get(String(params.id))
    if (!held) return refused(404, 'render_not_found', 'There is no such render.')
    held.polls += 1
    if (held.polls >= 1 && held.render.status === 'pending') {
      held.render = {
        ...held.render,
        status: 'done',
        colors: ['#1B6CA8', '#E8532F'],
        bbox_mm: { min: [-30, -10, 0], max: [30, 10, 6], size: [60, 20, 6] },
        preview_url: `${base}/renders/${held.render.id}/preview.glb`,
      }
    }
    return HttpResponse.json(held.render)
  }),
  http.get(`${base}/renders/:id/preview.glb`, () =>
    HttpResponse.arrayBuffer(keychainGlb(['#1B6CA8', '#E8532F'], { x: 60, y: 20, z: 6 }).slice().buffer as ArrayBuffer, {
      headers: { 'Content-Type': 'model/gltf-binary' },
    }),
  ),
  http.get(`${base}/renders/:id/estimate`, ({ params }) => {
    const held = renders.get(String(params.id))
    if (held?.render.status !== 'done') return refused(404, 'render_not_found', 'There is no such render.')
    // The slice works the price out first: one `pending` answer per render, as the shop
    // gives while its slice runs, then the price. The page reads again after its first
    // poll wait (`POLL.firstMs`, 1 s), so each render's price shows about 1 s later.
    held.estimates += 1
    if (held.estimates === 1) return HttpResponse.json({ ...PENDING_PRICE, retry_after: 0 })
    // #6921: the priced slice records the variant, with the output it saved.
    variants.set(held.variant, { price: ESTIMATE, preview: cachedPreview(hexKey(held.variant)), render: null, values: null })
    return HttpResponse.json(ESTIMATE)
  }),
  // #6921: the instant read. It checks the body as the render does, starts nothing, and
  // answers what the shop holds of the choices: a priced variant's price and preview, or none.
  http.post(`${base}/listings/:id/variant`, async ({ request, params }) => {
    const body = (await request.json()) as Body
    if (body.listing_id !== params.id) return refused(422, 'render_body_invalid', 'This request is not a render of a listing.')
    const checked = outside(body, false)
    if (checked instanceof Response) return checked
    const held = variants.get(variantOf(body))
    const answer: StoreVariant = held ?? { price: NO_PRICE, preview: null, render: null, values: null }
    return HttpResponse.json({ ...answer, values: colourPlan(checked, (body.values ?? {}) as Record<string, ParamValue>).shown })
  }),
  http.get(`${base}/variants/:key/preview.glb`, () =>
    HttpResponse.arrayBuffer(keychainGlb(['#1B6CA8', '#E8532F'], { x: 60, y: 20, z: 6 }).slice().buffer as ArrayBuffer, {
      headers: { 'Content-Type': 'model/gltf-binary' },
    }),
  ),
  http.get(`${base}/draft`, () => {
    // A cart read moves each model check on (`storefrontModels.ts`).
    if (draft) advanceChecks(draft)
    return HttpResponse.json(draft)
  }),
  http.post(`${base}/draft/items`, async ({ request }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const body = (await request.json()) as Body
    const checked = outside(body)
    if (checked instanceof Response) return checked
    const current = ensureDraft()
    current.items.push({
      id: `item-${next()}`,
      kind: 'model',
      title: checked.title,
      quantity: body.quantity ?? 1,
      inputs: (body.values ?? {}) as Record<string, string | number | boolean>,
      labels: Object.fromEntries(Object.keys(body.values ?? {}).map((name) => [name, checked.params[name]?.label ?? name])),
      notes: null,
      attachments: [],
      sources: [],
      own_model: false,
      // The mocks slice nothing: every line is priced by hand (#6153).
      price: 'by_hand',
    })
    save()
    return HttpResponse.json(current)
  }),
  http.delete(`${base}/draft/items/:id`, ({ request, params }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const current = ensureDraft()
    if (!current.items.some((item) => item.id === params.id)) {
      return refused(404, 'item_not_found', 'There is no such item.')
    }
    current.items = current.items.filter((item) => item.id !== params.id)
    save()
    return HttpResponse.json(current)
  }),
  // The bot check is off in the mocks (#5283): no site key, no widget.
  http.get(`${base}/turnstile`, () => HttpResponse.json({ site_key: null })),
  // The CRUD side's submit (`orders/storefront/submit.py`), as far as the page needs it.
  http.post(`${base}/draft/submit`, async ({ request }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const body = (await request.json()) as { draft_id: string; email: string; shipping_option_id?: string; address?: unknown }
    if (!draft || draft.id !== body.draft_id) return refused(404, 'draft_not_found', 'There is no such order.')
    if (draft.items.length === 0) return refused(409, 'draft_empty', 'Your order is empty. Add something to it first.')
    const option = SHIPPING.find((o) => o.id === body.shipping_option_id)
    if (!option) return refused(422, 'shipping_option_id', 'Choose how your order reaches you.')
    if (option.ships && !body.address) return refused(409, 'shipping_address_required', 'This way of shipping needs your address.')
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(body.email)) return refused(422, 'email_invalid', 'Give one email address, such as ada@example.com.')
    const submitted = { id: draft.id, number: `ORD-2026-${++orders}`, email: body.email, pay_now: false }
    draft = null
    save()
    return HttpResponse.json(submitted)
  }),
  http.post(`${base}/order/view`, async ({ request }) => {
    // The token `models` opens `storefrontModels.ts`'s order.
    const models = await modelsOrderOf(request)
    if (models) return HttpResponse.json(models)
    const found = await orderOf(request)
    return found instanceof Response ? found : HttpResponse.json(found)
  }),
  http.post(`${base}/order/revisions/:id/answer`, async ({ request, params }) => {
    const missing = keyMissing(request.clone())
    if (missing) return missing
    const found = await orderOf(request.clone())
    if (found instanceof Response) return found
    const body = (await request.json()) as { answer: 'accepted' | 'changes_requested'; note?: string }
    const item = found.items[0]!
    const revision = item.revisions.find((r) => r.id === params.id)
    if (!revision) return refused(404, 'revision_not_found', 'There is no such design.')
    if (revision.answer) return refused(409, 'revision_answered', 'This design was answered already. Load the page again.')
    if (body.answer === 'changes_requested' && !body.note?.trim()) return refused(422, 'note', 'Say what you would like changed.')
    Object.assign(revision, { answer: body.answer, answer_note: body.note ?? '', can_answer: false })
    if (body.answer === 'accepted') {
      item.status = 'Approved'
      item.quote = { ...item.quote!, accepted: true }
      order = {
        ...found,
        status: 'Accepted: we will start making it soon',
        can_cancel: false,
        payments: [
          { id: 'p1', kind: 'balance', amount_minor: 1250, currency: 'USD', status: 'open', pay_url: '/mock-pay/p1', manual: false },
          { id: 'p2', kind: 'deposit', amount_minor: 500, currency: 'USD', status: 'open', pay_url: null, manual: true },
        ],
        payment_instructions: 'Venmo @example-shop\nOr pay cash when you collect it.',
      }
    }
    return HttpResponse.json(order)
  }),
  http.post(`${base}/order/cancel`, async ({ request }) => {
    const missing = keyMissing(request.clone())
    if (missing) return missing
    const found = await orderOf(request)
    if (found instanceof Response) return found
    if (found.payments.some((p) => p.status === 'paid')) {
      return refused(409, 'order_paid', 'You have paid part of this order. Ask the shop to cancel it.')
    }
    if (!found.can_cancel) return refused(409, 'order_approved', 'Part of your order is approved already. Ask the shop to cancel it.')
    order = { ...found, status: 'Cancelled', can_cancel: false, items: found.items.map((i) => ({ ...i, status: 'Cancelled' })) }
    return HttpResponse.json(order)
  }),
  http.post(`${base}/draft/requests`, async ({ request }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const form = await request.formData()
    const text = String(form.get('text') ?? '')
    const files = form.getAll('files').filter((f): f is File => typeof f !== 'string')
    const current = ensureDraft()
    current.items.push({
      id: `item-${next()}`,
      kind: 'request',
      title: 'Design request',
      quantity: 1,
      inputs: null,
      labels: {},
      notes: text,
      attachments: files.map((file) => ({ filename: file.name, kind: 'image', url: `/mock-files/${encodeURIComponent(file.name)}` })),
      sources: [],
      own_model: false,
      // The mocks slice nothing: every line is priced by hand (#6153).
      price: 'by_hand',
    })
    save()
    return HttpResponse.json(current)
  }),
]

export function reset(): void {
  draft = null
  order = freshOrder()
  orders = 0
  renders = new Map()
  variants = new Map()
  fileReads = new Map()
  filePriced = new Set()
  serial = 0
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // No storage.
  }
}

load()
