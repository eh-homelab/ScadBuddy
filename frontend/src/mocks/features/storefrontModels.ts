import { http, HttpResponse } from 'msw'
import type { CustomerOrder, Draft, DraftSource } from '../../storefront/api/client'
import { ensureDraft, keyMissing, next, refused, save } from './storefrontPublic'

/**
 * #6133 PR 4 (spec 2026-11-13 §4, §6.2, §6.3, §10.5): "Print my own model" over the mocks.
 * `POST /uploads` answers a slot per file on the mock bucket (`BUCKET`), whose `PUT`
 * answers 200; `POST /draft/models` adds a "Your models" request to the cart. Each file
 * reads `checking` on the first cart read after the request, then `ready` with a preview,
 * or `refused` when its name holds "broken", as the intake check would. The colours are the
 * price book's two blue and orange filaments. The token `models` (`/o#models`) opens an
 * order whose request has a ready file, a file the order's end removed, a link, a recorded
 * purchase and a pending one.
 */

const base = '/api/store/v1'
/**
 * The mock bucket's staging prefix. In production the bucket is another origin, which the
 * edge's CSP names in `connect-src`. `vite preview` sends the back office's CSP
 * (`page-csp.txt`) on the storefront too, which names no bucket, so the mock bucket is on
 * the page's own origin. `ModelForm.test.tsx` covers the cross-origin PUT and its preflight.
 */
export const BUCKET = '/mock-bucket/staging'
export const MODELS_TOKEN = 'models'
const ENDED = 'The order ended; the staged file was removed. Upload it again.'
/** A one-pixel PNG, the bytes the mock "check" stored. */
const PNG = Uint8Array.from(
  atob(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
  ),
  (c) => c.charCodeAt(0),
)
const CORS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'PUT',
  'Access-Control-Allow-Headers': 'content-type, x-amz-acl',
  'Access-Control-Expose-Headers': 'ETag',
}
const FILAMENTS = [
  { id: 'f-blue', colour: '#1B6CA8', colour_name: 'Ocean blue', material: 'PLA' },
  { id: 'f-orange', colour: '#E8532F', colour_name: 'Signal orange', material: 'PLA' },
]

/** The slots answered, by id: their names, for the request that claims them. */
let slots = new Map<string, string>()
/** How many cart reads each checking file has seen. */
let reads = new Map<string, number>()

function modelsOrder(): CustomerOrder {
  return {
    number: 'ORD-2026-9',
    status: 'We are reviewing your order',
    currency: 'USD',
    minor_exponent: 2,
    can_cancel: true,
    items: [
      {
        id: 'om1',
        title: 'Your models',
        quantity: 2,
        status: 'We are working on the design',
        revisions: [],
        quote: null,
        sources: [
          {
            kind: 'file',
            purpose: 'print',
            filename: 'bracket.3mf',
            url: null,
            note: '',
            status: 'open',
            amount_minor: null,
            shop_note: '',
            preview_url: `${base}/order/sources/s-ready/preview`,
            customer_reason: null,
          },
          {
            kind: 'file',
            purpose: 'print',
            filename: 'lid.stl',
            url: null,
            note: '',
            status: 'declined',
            amount_minor: null,
            shop_note: '',
            preview_url: null,
            customer_reason: ENDED,
          },
          {
            kind: 'link',
            purpose: 'print',
            filename: null,
            url: 'https://www.printables.com/model/77-hinge',
            note: '',
            status: 'manual',
            amount_minor: null,
            shop_note: '',
            preview_url: null,
            customer_reason: null,
          },
          {
            kind: 'link',
            purpose: 'purchase',
            filename: null,
            url: 'https://parts.example.com/magnets',
            note: '6 mm magnets, 20 of them',
            status: 'recorded',
            amount_minor: 450,
            shop_note: '',
            preview_url: null,
            customer_reason: null,
          },
          {
            kind: 'link',
            purpose: 'purchase',
            filename: null,
            url: 'https://parts.example.com/screws',
            note: 'M3 screws',
            status: 'pending',
            amount_minor: null,
            shop_note: '',
            preview_url: null,
            customer_reason: null,
          },
        ],
      },
    ],
    payments: [],
    payment_instructions: null,
    shipments: [],
    charges: [
      { kind: 'purchase', label: 'Bought for your project: 6 mm magnets, 20 of them', amount_minor: 450 },
      { kind: 'handling', label: 'Handling for bought items', amount_minor: 100 },
    ],
  }
}

let order = modelsOrder()

/** The models order for a body that names its token; null for any other reader. */
export async function modelsOrderOf(request: Request): Promise<CustomerOrder | null> {
  const body = (await request.clone().json().catch(() => ({}))) as { token?: unknown }
  return body.token === MODELS_TOKEN ? order : null
}

/**
 * Each checking file moves on one step per cart read, as the intake check would.
 * `storefrontPublic.ts`'s `GET /draft` calls it: one route has one feature module.
 */
export function advanceChecks(draft: Draft): void {
  let moved = false
  for (const item of draft.items) {
    for (const source of item.sources ?? []) {
      if (source.kind !== 'file' || source.check_status !== 'checking') continue
      const seen = (reads.get(source.id) ?? 0) + 1
      reads.set(source.id, seen)
      if (seen < 2) continue
      moved = true
      if (source.filename?.includes('broken')) {
        Object.assign(source, { check_status: 'refused', check_reason: 'This file is not a 3MF: it is not a readable zip.' })
      } else {
        Object.assign(source, { check_status: 'ready', preview_url: `${base}/draft/sources/${source.id}/preview` })
      }
    }
  }
  if (moved) save()
}

interface ModelBody {
  uploads?: string[]
  links?: { url: string; purpose?: 'print' | 'purchase'; note?: string }[]
  quantity?: number
  note?: string
}

export const handlers = [
  http.get(`${base}/models/colours`, () => HttpResponse.json({ filaments: FILAMENTS })),
  http.post(`${base}/uploads`, async ({ request }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const body = (await request.json()) as { files: { filename: string; media_type: string }[] }
    const expiresAt = new Date(Date.now() + 15 * 60_000).toISOString()
    return HttpResponse.json({
      uploads: body.files.map((file) => {
        const id = next()
        slots.set(id, file.filename)
        return {
          id,
          filename: file.filename,
          url: `${BUCKET}/${id}?X-Amz-Signature=mock`,
          headers: { 'Content-Type': file.media_type, 'x-amz-acl': 'private' },
          expires_at: expiresAt,
        }
      }),
    })
  }),
  // The bucket's CORS rule (spec §6.4): a preflight, then the PUT.
  http.options(`${BUCKET}/:id`, () => new HttpResponse(null, { status: 204, headers: CORS })),
  http.put(`${BUCKET}/:id`, () => new HttpResponse(null, { status: 200, headers: CORS })),
  http.post(`${base}/draft/models`, async ({ request }) => {
    const missing = keyMissing(request)
    if (missing) return missing
    const body = (await request.json()) as ModelBody
    const files = body.uploads ?? []
    if (files.some((id) => !slots.has(id))) {
      return refused(422, 'upload_missing', 'A file did not arrive. Upload it again.')
    }
    const links = body.links ?? []
    if (files.length === 0 && !links.some((link) => (link.purpose ?? 'print') === 'print')) {
      return refused(422, 'model_request_empty', 'Add a model file or a link to a model to print.')
    }
    const sources: DraftSource[] = [
      ...files.map((id) => ({
        id: `src-${id}`,
        kind: 'file' as const,
        purpose: 'print' as const,
        filename: slots.get(id)!,
        size: 1024,
        url: null,
        note: '',
        check_status: 'checking' as const,
        check_reason: null,
        status: 'open' as const,
        preview_url: null,
      })),
      ...links.map((link, index) => ({
        id: `src-link-${next()}-${index}`,
        kind: 'link' as const,
        purpose: link.purpose ?? 'print',
        filename: null,
        size: null,
        url: link.url,
        note: link.note ?? '',
        check_status: null,
        check_reason: null,
        status: (link.purpose === 'purchase' ? 'pending' : 'manual') as DraftSource['status'],
        preview_url: null,
      })),
    ]
    for (const id of files) slots.delete(id)
    const draft = ensureDraft()
    draft.items.push({
      id: `item-${next()}`,
      kind: 'request',
      title: 'Your models',
      quantity: body.quantity ?? 1,
      inputs: null,
      labels: {},
      notes: body.note ?? '',
      attachments: [],
      sources,
      own_model: true,
    })
    save()
    return HttpResponse.json(draft)
  }),
  http.get(`${base}/draft/sources/:id/preview`, () =>
    HttpResponse.arrayBuffer(PNG.buffer, {
      headers: { 'Content-Type': 'image/png', 'Content-Disposition': 'inline', 'X-Content-Type-Options': 'nosniff' },
    }),
  ),
  http.post(`${base}/order/sources/:id/preview`, async ({ request, params }) => {
    const found = await modelsOrderOf(request)
    if (!found || params.id !== 's-ready') return new HttpResponse(null, { status: 404 })
    return HttpResponse.arrayBuffer(PNG.buffer, {
      headers: { 'Content-Type': 'image/png', 'Content-Disposition': 'inline', 'X-Content-Type-Options': 'nosniff' },
    })
  }),
]

export function reset(): void {
  slots = new Map()
  reads = new Map()
  order = modelsOrder()
}
