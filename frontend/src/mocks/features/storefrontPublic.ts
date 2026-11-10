import { http, HttpResponse } from 'msw'
import type { Draft, Listing, StoreEstimate, StoreRender } from '../../storefront/api/client'
import { keychainGlb } from '../glb'
import { problem } from '../handlers'

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
 */

const base = '/api/store/v1'
const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'
const STORAGE_KEY = 'sb-mock-storefront-draft'

export const KEYCHAIN_ID = 'l1'
export const TAG_ID = 'l2'
const BLUE = { id: 'f-blue', colour: '#1B6CA8', colour_name: 'Ocean blue', material: 'PLA' }
const ORANGE = { id: 'f-orange', colour: '#E8532F', colour_name: 'Signal orange', material: 'PLA' }

export const KEYCHAIN: Listing = {
  id: KEYCHAIN_ID,
  title: 'Name keychain',
  description: 'A keychain with your name raised on it, in two colours.',
  params: {
    name: { kind: 'text', label: 'Name', group: '', colour: false, max_length: 20 },
    size: { kind: 'number', label: 'Length (mm)', group: '', colour: false, min: 40, max: 80, step: 5 },
    base_colour: { kind: 'choice', label: 'Base colour', group: '', colour: true, choices: ['#1B6CA8', '#E8532F'] },
  },
  colours: [BLUE, ORANGE],
  media: [],
  updated_at: '2026-11-07T12:00:00Z',
}

export const TAG: Listing = {
  id: TAG_ID,
  title: 'Luggage tag',
  description: 'A tag for a bag, in one colour.',
  params: {
    colour: { kind: 'choice', label: 'Colour', group: '', colour: true, choices: [] },
  },
  colours: [],
  media: [],
  updated_at: '2026-11-07T12:00:00Z',
}

const LISTINGS = [KEYCHAIN, TAG]

export const ESTIMATE: StoreEstimate = {
  estimate: true,
  price_minor: 1240,
  currency: 'USD',
  minor_exponent: 2,
  grams: '9.40',
  parts: [],
  unknown_parts: 0,
}

/** The shop's shipping options (backend `storefront_shipping_options`): one ships, one does not. */
const SHIPPING = [
  { id: 'post', label: 'Post', amount_minor: 700, ships: true },
  { id: 'pickup', label: 'Pick up at the workshop', amount_minor: 0, ships: false },
]
let orders = 0

let draft: Draft | null = null
let renders = new Map<string, { render: StoreRender; polls: number }>()
let serial = 0

function save(): void {
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

function next(): string {
  serial += 1
  return serial.toString(16).padStart(32, '0')
}

function refused(status: number, slug: string, detail: string) {
  return problem(status, status === 404 ? 'Not Found' : 'Unprocessable Content', detail, { type: PROBLEMS + slug })
}

function keyMissing(request: Request) {
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

/** The CRUD side's checks (`orders/storefront/inputs.py`), as far as the mocks need them. */
function outside(body: Body): Response | Listing {
  const listing = LISTINGS.find((l) => l.id === body.listing_id)
  if (!listing) return refused(404, 'listing_not_found', 'There is no such listing.')
  for (const [name, value] of Object.entries(body.values ?? {})) {
    const bound = listing.params[name]
    if (!bound) return refused(422, 'input_outside_listing', 'This choice is not offered for this listing.')
    if (bound.kind === 'choice' && !bound.choices.includes(value as string)) {
      return refused(422, 'input_outside_listing', 'This choice is not offered for this listing.')
    }
  }
  const archived = Object.values(listing.params).some((p) => p.kind === 'choice' && p.colour && p.choices.length === 0)
  if (archived) return refused(422, 'input_outside_listing', 'This choice is not offered for this listing.')
  return listing
}

function ensureDraft(): Draft {
  draft ??= {
    id: 'draft-1',
    currency: 'USD',
    minor_exponent: 2,
    shipping_currency: 'USD',
    shipping_minor_exponent: 2,
    items: [],
    shipping_options: SHIPPING,
  }
  return draft
}

export const handlers = [
  http.get(`${base}/listings`, () => HttpResponse.json({ listings: LISTINGS, requests_enabled: true })),
  http.get(`${base}/listings/:id`, ({ params }) => {
    const listing = LISTINGS.find((l) => l.id === params.id)
    return listing ? HttpResponse.json(listing) : refused(404, 'listing_not_found', 'There is no such listing.')
  }),
  http.post(`${base}/renders`, async ({ request }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const checked = outside((await request.json()) as Body)
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
    }
    renders.set(id, { render, polls: 0 })
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
  http.get(`${base}/renders/:id/estimate`, ({ params }) =>
    renders.get(String(params.id))?.render.status === 'done'
      ? HttpResponse.json(ESTIMATE)
      : refused(404, 'render_not_found', 'There is no such render.'),
  ),
  http.get(`${base}/draft`, () => HttpResponse.json(draft)),
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
    const submitted = { id: draft.id, number: `ORD-2026-${++orders}`, email: body.email }
    draft = null
    save()
    return HttpResponse.json(submitted)
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
    })
    save()
    return HttpResponse.json(current)
  }),
]

export function reset(): void {
  draft = null
  orders = 0
  renders = new Map()
  serial = 0
  try {
    sessionStorage.removeItem(STORAGE_KEY)
  } catch {
    // No storage.
  }
}

load()
