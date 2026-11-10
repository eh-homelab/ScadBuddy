/**
 * The public storefront page's client (#5282, spec 2026-11-02 §5.1): the edge's routes
 * under `/api/store/v1`, typed from the edge's own spec (`schema.d.ts`, written by
 * `pnpm gen:api` from `export_openapi --storefront`; never committed). It shares nothing
 * with the back office's `api/client`.
 *
 * Every write carries an `Idempotency-Key`: one UUID per visitor action, the same one when
 * that action is sent again (`ActionKey`). The edge requires it (428 without) and forwards
 * it bound to the visitor (`backend/scadbuddy/storefront/routes.py` `_request_key`), and
 * the CRUD side keeps one answer per key, so a render, an add, a removal or a design
 * request or a submit sent again after a lost answer is done once. The customer's order page
 * sends the order link's token, or a signed-in reader's order id, in each body (`order*`),
 * never in a path; a reader that reads nothing is a 404 with no body. A refusal is a `StoreError` with the problem's
 * `type` and `detail` (`https://scadbuddy.dev/problems/storefront/*`) and, on a 429, the
 * seconds the edge says to wait.
 */
import type { components, paths } from './schema'

type Schemas = components['schemas']
export type Listing = Schemas['PublicListing']
export type Listings = Schemas['PublicListings']
export type PublicParam = Listing['params'][string]
export type Filament = Schemas['PublicFilament']
export type StoreRender = Schemas['StoreRender']
export type StoreEstimate = Schemas['StoreEstimate']
export type Draft = Schemas['DraftView']
export type DraftItem = Schemas['DraftItem']
export type TurnstileSiteKey = Schemas['TurnstileSiteKey']
export type ShippingOption = Schemas['PublicShippingOption']
export type SubmittedOrder = Schemas['SubmittedOrder']
export type CustomerOrder = Schemas['CustomerOrderView']
/**
 * Who reads an order: the order link's token (`/o#<token>`), or, signed in, the order's
 * id (`/o?order=<id>`), which the CRUD side reads only for the session's own account.
 */
export type OrderReader = { token: string } | { order_id: string }
/** What the pay page needs to render PayPal's buttons for one request (`/pay#<token>`). */
export type PayCheckout = Schemas['VenmoCheckout']
export type CustomerItem = Schemas['CustomerItem']
export type CustomerRevision = Schemas['CustomerRevision']
export type CustomerPayment = Schemas['CustomerPayment']
export type ParamValue = boolean | number | string
// Accounts (#5283, spec §5.2).
export type Account = Schemas['AccountView']
export type AccountOrder = Schemas['AccountOrder']
export type AccountOrders = Schemas['AccountOrders']

/** An order on "Your orders" links to the order page by its id (agreed on #5283). */
export function ACCOUNT_ORDER_URL(orderId: string): string {
  return `/o?order=${encodeURIComponent(orderId)}`
}

export const BASE = '/api/store/v1'
export const PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'

/** What a visitor reads when the edge said nothing more useful. */
const FALLBACK = 'The shop could not answer. Try again in a moment.'

export class StoreError extends Error {
  readonly status: number
  /** The problem's `type`, or null when the answer was not a problem document. */
  readonly type: string | null
  /** Seconds to wait, from `Retry-After`; null when the edge sent none. */
  readonly retryAfter: number | null

  constructor(status: number, type: string | null, detail: string, retryAfter: number | null) {
    super(detail)
    this.name = 'StoreError'
    this.status = status
    this.type = type
    this.retryAfter = retryAfter
  }

  /** The problem's slug: `render_limited` for `…/storefront/render_limited`. */
  get slug(): string | null {
    return this.type?.startsWith(PROBLEMS) ? this.type.slice(PROBLEMS.length) : null
  }
}

function retryAfter(response: Response): number | null {
  const header = response.headers.get('Retry-After')
  if (header === null) return null
  const seconds = Number(header)
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds)
  const date = Date.parse(header)
  return Number.isNaN(date) ? null : Math.max(0, Math.ceil((date - Date.now()) / 1000))
}

async function refusal(response: Response): Promise<StoreError> {
  let type: string | null = null
  let detail = FALLBACK
  try {
    const body = (await response.json()) as { type?: unknown; detail?: unknown }
    if (typeof body.type === 'string') type = body.type
    if (typeof body.detail === 'string' && body.detail.trim()) detail = body.detail
  } catch {
    // Not JSON: the fallback text stands.
  }
  return new StoreError(response.status, type, detail, retryAfter(response))
}

interface CallOptions {
  method?: 'GET' | 'POST' | 'DELETE'
  json?: unknown
  form?: FormData
  key?: string
  /** The bot check's token (`components/Turnstile.tsx`), for a form that takes one. */
  turnstile?: string | null
}

async function call<T>(path: string, { method = 'GET', json, form, key, turnstile }: CallOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' }
  if (key) headers['Idempotency-Key'] = key
  if (turnstile) headers['X-Turnstile-Token'] = turnstile
  let body: BodyInit | undefined
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(json)
  } else if (form) {
    body = form
  }
  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, { method, headers, body, credentials: 'same-origin' })
  } catch {
    throw new StoreError(0, null, 'The shop is out of reach. Check your connection and try again.', null)
  }
  if (!response.ok) throw await refusal(response)
  // A 204 (sign-out) has no body.
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export interface RenderInput {
  listing_id: string
  listing_updated_at: string
  /** Only the parameters the listing offers: never a locked one (spec §5.4). */
  values: Record<string, ParamValue>
  /** The listing filament for each colour slot of the render, by slot. */
  colours: Record<string, string>
}

/** A submit of the cart the page shows: the edge's request body, from its spec. */
export type SubmitInput = paths['/api/store/v1/draft/submit']['post']['requestBody']['content']['application/json']

export const store = {
  listings: () => call<Listings>('/listings'),
  listing: (id: string) => call<Listing>(`/listings/${encodeURIComponent(id)}`),
  startRender: (input: RenderInput, key: string) => call<StoreRender>('/renders', { method: 'POST', json: input, key }),
  render: (jobId: string) => call<StoreRender>(`/renders/${encodeURIComponent(jobId)}`),
  estimate: (jobId: string) => call<StoreEstimate>(`/renders/${encodeURIComponent(jobId)}/estimate`),
  draft: () => call<Draft | null>('/draft'),
  addItem: (input: RenderInput & { quantity: number }, key: string) =>
    call<Draft>('/draft/items', { method: 'POST', json: input, key }),
  removeItem: (itemId: string, key: string) =>
    call<Draft>(`/draft/items/${encodeURIComponent(itemId)}`, { method: 'DELETE', key }),
  addRequest: (text: string, files: readonly File[], key: string, turnstile: string | null = null) => {
    const form = new FormData()
    form.append('text', text)
    for (const file of files) form.append('files', file, file.name)
    return call<Draft>('/draft/requests', { method: 'POST', form, key, turnstile })
  },
  submit: (input: SubmitInput, key: string, turnstile: string | null = null) =>
    call<SubmittedOrder>('/draft/submit', { method: 'POST', json: input, key, turnstile }),
  turnstile: () => call<TurnstileSiteKey>('/turnstile'),
  payCheckout: (token: string) => call<PayCheckout>('/pay/checkout', { method: 'POST', json: { token } }),
  orderView: (who: OrderReader) => call<CustomerOrder>('/order/view', { method: 'POST', json: { ...who } }),
  answerRevision: (
    who: OrderReader,
    revisionId: string,
    answer: 'accepted' | 'changes_requested',
    note: string,
    key: string,
  ) =>
    call<CustomerOrder>(`/order/revisions/${encodeURIComponent(revisionId)}/answer`, {
      method: 'POST',
      json: { ...who, answer, note },
      key,
    }),
  acceptQuote: (who: OrderReader, quoteId: string, key: string) =>
    call<CustomerOrder>(`/order/quotes/${encodeURIComponent(quoteId)}/accept`, { method: 'POST', json: { ...who }, key }),
  cancelOrder: (who: OrderReader, reason: string, key: string) =>
    call<CustomerOrder>('/order/cancel', { method: 'POST', json: { ...who, reason }, key }),
  // Accounts: the session is the edge's HttpOnly cookie, never in a body. Verify is sent
  // once (a code is single-use), so it takes no Idempotency-Key.
  account: () => call<Account | null>('/account'),
  accountOrders: () => call<AccountOrders>('/account/orders'),
  signIn: (email: string, code: string, turnstile: string | null = null) =>
    call<Account>('/account/verify', { method: 'POST', json: { email, code }, turnstile }),
  signOut: () => call<void>('/account/sign-out', { method: 'POST' }),
}

/**
 * A random UUID v4. `crypto.randomUUID` exists only in a secure context (https or
 * localhost); over plain http (a LAN preview) the key is built from
 * `crypto.getRandomValues`, which every context has, with the version 4 and variant bits
 * set (https://www.rfc-editor.org/rfc/rfc9562#section-5.4).
 */
function newKey(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = (bytes[6]! & 0x0f) | 0x40
  bytes[8] = (bytes[8]! & 0x3f) | 0x80
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

/**
 * One `Idempotency-Key` per visitor action: `for(what)` answers the same key while `what`
 * (the action and its inputs, as one string) stays the same and `done()` was not called,
 * so a retry of a failed or lost request is the same request to the shop. A different
 * action, or one after `done()`, gets a fresh key.
 */
export class ActionKey {
  private current: { what: string; key: string } | null = null

  for(what: string): string {
    if (this.current?.what !== what) this.current = { what, key: newKey() }
    return this.current.key
  }

  done(): void {
    this.current = null
  }
}

/** The text a visitor reads for `error`. */
export function errorText(error: unknown): string {
  return error instanceof StoreError ? error.message : FALLBACK
}
