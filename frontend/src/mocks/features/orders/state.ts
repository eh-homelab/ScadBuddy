import { HttpResponse } from 'msw'
import type {
  Customer,
  CustomerAddress,
  Filament,
  Fulfilment,
  Order,
  OrderCharge,
  OrderEvent,
  OrderAttachment,
  OrderItem,
  Payment,
  Quote,
  Shipment,
} from '../../../api/types'
import { versionIds } from '../../fixtures'

/**
 * The in-memory store the orders mocks share (#4249): customers, orders, items, quotes,
 * attempts, shipments, charges, payments and the audit line, as the backend's tables
 * hold them (`orders/store.py`), with the lifecycle's own derivation and refusals
 * (`orders/lifecycle.py`) mirrored by `orders.ts`. `reset` puts the fixtures back.
 *
 * Every `.ts` under `features/` is loaded as a mock feature and must export `handlers`
 * (`mocks/features.ts`): this one answers no route of its own.
 */
export const handlers = []

export type Problem = { status: number; type: string; detail: string; extensions?: Record<string, unknown> }

export const PROBLEMS = 'https://scadbuddy.dev/problems/orders/'
export const UNKNOWN_PARAMETER = 'https://scadbuddy.dev/problems/unknown-parameter'
export const BAD_CURSOR = 'https://scadbuddy.dev/problems/bad-cursor'
const TITLES: Record<number, string> = { 400: 'Bad Request', 404: 'Not Found', 409: 'Conflict', 422: 'Unprocessable Content' }

/** An RFC 9457 problem, as the backend answers every error. */
export function problem(request: Request, status: number, detail: string, type = 'about:blank', extensions: Record<string, unknown> = {}) {
  return HttpResponse.json(
    { type, title: TITLES[status] ?? 'Error', status, detail, instance: new URL(request.url).pathname, ...extensions },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

/** A field refusal, as FastAPI's `_validation_error` and the orders routes shape one. */
export function fieldProblem(request: Request, field: string, msg: string) {
  return problem(request, 422, `${field}: ${msg}`, 'about:blank', { errors: [{ loc: ['body', field], msg }] })
}

/** The lifecycle's refusal (`LifecycleError`), typed by its slug with the ids in the way. */
export class Refused extends Error {
  readonly slug: string
  readonly items: string[]
  readonly notFound: boolean
  /** The shipments in the way (`order_item_shipped`), as the backend's `shipments`. */
  readonly shipments: string[]
  constructor(slug: string, message: string, items: string[] = [], notFound = false, shipments: string[] = []) {
    super(message)
    this.slug = slug
    this.items = items
    this.notFound = notFound
    this.shipments = shipments
  }
}

export type State = {
  customers: Customer[]
  orders: Order[]
  items: OrderItem[]
  quotes: Quote[]
  fulfilments: Fulfilment[]
  shipments: Shipment[]
  charges: OrderCharge[]
  payments: Payment[]
  events: OrderEvent[]
  /** The year's last number (`order_numbers`). */
  numbers: Record<string, number>
  /** The price book's filament records (#4250), archived ones included. */
  filaments: Filament[]
  /** An order's files and its items' (#4251), with each one's bytes. */
  attachments: OrderAttachment[]
  files: Record<string, Blob>
}

let counter = 0
export const newId = (prefix: string) => `${prefix}-${(++counter).toString(16).padStart(8, '0')}`
export const now = () => new Date().toISOString()
export const BROWSER = { kind: 'browser' as const, principal: null, session: null }

export const CUSTOMER_ADA = 'c1a00000-0000-4000-8000-000000000001'
export const CUSTOMER_BOB = 'c1a00000-0000-4000-8000-000000000002'
export const ADDRESS_ADA = 'a1a00000-0000-4000-8000-000000000001'
export const ORDER_DRAFT = '0e5a3c1e-1111-4222-8333-944455556661'
export const ORDER_ACCEPTED = '0e5a3c1e-1111-4222-8333-944455556662'
export const ORDER_CLOSED = '0e5a3c1e-1111-4222-8333-944455556663'
export const ITEM_DRAFT = '7b1c2d3e-4f50-4617-8829-aabbccddee01'
export const ITEM_APPROVED = '7b1c2d3e-4f50-4617-8829-aabbccddee02'
export const ITEM_CLOSED = '7b1c2d3e-4f50-4617-8829-aabbccddee03'
export const QUOTE_ACCEPTED = 'q0000000-0000-4000-8000-000000000002'
/** The fixture output "Reagan" of name-keychain, which the draft item was made from. */
export const OUTPUT_REAGAN = 'a'.repeat(32)

function address(id: string, customerId: string): CustomerAddress {
  void customerId
  return {
    id,
    label: 'home',
    recipient: 'Ada Lovelace',
    lines: ['1 Analytical Way'],
    locality: 'London',
    region: null,
    postal_code: 'N1 1AA',
    country: 'GB',
    phone: null,
    default_shipping: true,
    default_billing: true,
    position: 0,
  }
}

/**
 * The minor unit's decimal places of a currency, as the backend's table has them (ISO 4217,
 * `backend/scadbuddy/orders/currency.py`); the mock answers `minor_exponent` from it, and the
 * pages take what they are answered, as they do from the backend.
 */
export function minorExponent(currency: string): number {
  if (['BIF', 'CLP', 'DJF', 'GNF', 'ISK', 'JPY', 'KMF', 'KRW', 'PYG', 'RWF', 'UGX', 'UYI', 'VND', 'VUV', 'XAF', 'XOF', 'XPF'].includes(currency)) return 0
  if (['BHD', 'IQD', 'JOD', 'KWD', 'LYD', 'OMR', 'TND'].includes(currency)) return 3
  if (['CLF', 'UYW'].includes(currency)) return 4
  return 2
}

export const FILAMENT_PLA = 'f1a00000-0000-4000-8000-000000000001'
export const FILAMENT_PETG = 'f1a00000-0000-4000-8000-000000000002'

function filament(id: string, material: string, colour: string, colourName: string, cost: string): Filament {
  return {
    id,
    material,
    subtype: 'Basic',
    brand: 'Bambu',
    colour_name: colourName,
    colour,
    cost_per_kg: cost,
    density_g_cm3: '1.24',
    archived_at: null,
    created_at: '2026-10-18T10:00:00Z',
    updated_at: '2026-10-18T10:00:00Z',
  }
}

function order(id: string, number: string, customerId: string, status: Order['status'], at: string, extra: Partial<Order> = {}): Order {
  return {
    id,
    number,
    customer_id: customerId,
    status,
    currency: 'USD',
    minor_exponent: 2,
    on_hold: false,
    shipping_address_id: customerId === CUSTOMER_ADA ? ADDRESS_ADA : null,
    billing_address_id: null,
    shipping_address: null,
    billing_address: null,
    notes: '',
    due_at: null,
    accepted_at: null,
    closed_at: null,
    cancelled_at: null,
    created_at: at,
    updated_at: at,
    created_by: BROWSER,
    ...extra,
  }
}

export function item(id: string, orderId: string, status: OrderItem['status'], extra: Partial<OrderItem> = {}): OrderItem {
  return {
    id,
    order_id: orderId,
    position: 0,
    status,
    quantity: 1,
    title: 'Name Keychain',
    notes: '',
    reference: {
      slug: 'name-keychain',
      commit: versionIds.added,
      inputs: { params: { name: 'Reagan' }, v: 1 },
      libraries: [],
      template_name: 'Name Keychain',
      render_key: 'c'.repeat(64),
    },
    colours: [
      { slot: 0, colour: '#f9d72c', filament_id: null },
      { slot: 1, colour: '#1e1e1e', filament_id: null },
    ],
    output_id: OUTPUT_REAGAN,
    created_at: '2026-10-19T09:00:00Z',
    updated_at: '2026-10-19T09:00:00Z',
    ...extra,
  }
}

function fixtures(): State {
  const { id: _a, ...adaAddress } = address(ADDRESS_ADA, CUSTOMER_ADA)
  const snapshot = { ...adaAddress, id: ADDRESS_ADA }
  delete (snapshot as { position?: number }).position
  return {
    customers: [
      {
        id: CUSTOMER_ADA,
        display_name: 'Ada Lovelace',
        company: 'Analytical Engines',
        notes: 'prefers email',
        tags: ['vip'],
        created_at: '2026-10-18T10:00:00Z',
        updated_at: '2026-10-18T10:00:00Z',
        archived_at: null,
        contacts: [
          { id: 'k1a00000-0000-4000-8000-000000000001', kind: 'email', value: 'ada@example.test', label: null, primary: true, position: 0 },
          { id: 'k1a00000-0000-4000-8000-000000000002', kind: 'phone', value: '+44 20 0000 0001', label: 'work', primary: true, position: 1 },
        ],
        addresses: [address(ADDRESS_ADA, CUSTOMER_ADA)],
      },
      {
        id: CUSTOMER_BOB,
        display_name: 'Bob Builder',
        company: null,
        notes: '',
        tags: [],
        created_at: '2026-10-18T11:00:00Z',
        updated_at: '2026-10-18T11:00:00Z',
        archived_at: null,
        contacts: [{ id: 'k1a00000-0000-4000-8000-000000000003', kind: 'instagram', value: '@bob', label: null, primary: false, position: 0 }],
        addresses: [],
      },
    ],
    orders: [
      order(ORDER_CLOSED, 'ORD-2026-1', CUSTOMER_BOB, 'closed', '2026-10-17T09:00:00Z', {
        accepted_at: '2026-10-17T10:00:00Z',
        closed_at: '2026-10-18T10:00:00Z',
      }),
      order(ORDER_ACCEPTED, 'ORD-2026-2', CUSTOMER_ADA, 'accepted', '2026-10-18T12:00:00Z', {
        accepted_at: '2026-10-18T13:00:00Z',
        shipping_address: snapshot,
        notes: 'Gift wrap, please.',
      }),
      order(ORDER_DRAFT, 'ORD-2026-3', CUSTOMER_ADA, 'draft', '2026-10-19T09:00:00Z'),
    ],
    items: [
      item(ITEM_CLOSED, ORDER_CLOSED, 'fulfilled', { output_id: null }),
      item(ITEM_APPROVED, ORDER_ACCEPTED, 'approved', { quantity: 2 }),
      item(ITEM_DRAFT, ORDER_DRAFT, 'draft', { quantity: 2 }),
    ],
    quotes: [
      {
        id: QUOTE_ACCEPTED,
        order_item_id: ITEM_APPROVED,
        created_at: '2026-10-18T12:30:00Z',
        source: 'manual',
        accepted: true,
        sliced_sha256: null,
        sliced_blob_key: null,
        plates: [],
        lines: [{ kind: 'manual', label: 'Quoted by hand', quantity: 1, unit_minor: 1500, amount_minor: 1500 }],
        unit_cost_minor: 1500,
        unit_price_minor: 1500,
        quantity: 2,
        total_minor: 3000,
        created_by: BROWSER,
      },
      {
        id: 'q0000000-0000-4000-8000-000000000001',
        order_item_id: ITEM_CLOSED,
        created_at: '2026-10-17T09:30:00Z',
        source: 'manual',
        accepted: true,
        sliced_sha256: null,
        sliced_blob_key: null,
        plates: [],
        lines: [{ kind: 'manual', label: 'Quoted by hand', quantity: 1, unit_minor: 900, amount_minor: 900 }],
        unit_cost_minor: 900,
        unit_price_minor: 900,
        quantity: 1,
        total_minor: 900,
        created_by: BROWSER,
      },
    ],
    fulfilments: [
      {
        id: 'f0000000-0000-4000-8000-000000000001',
        order_item_id: ITEM_CLOSED,
        attempt: 1,
        quantity: 1,
        outcome: 'succeeded',
        failure_reason: null,
        started_at: null,
        finished_at: null,
        notes: '',
        accepted_at: '2026-10-17T18:00:00Z',
        created_by: BROWSER,
        created_at: '2026-10-17T17:00:00Z',
      },
    ],
    shipments: [
      {
        id: 's0000000-0000-4000-8000-000000000001',
        order_id: ORDER_CLOSED,
        carrier: 'Post',
        service: null,
        tracking: 'T-1',
        shipped_at: '2026-10-18T09:00:00Z',
        address: null,
        cost_minor: 500,
        notes: '',
        created_at: '2026-10-18T09:00:00Z',
        items: [{ order_item_id: ITEM_CLOSED, quantity: 1 }],
      },
    ],
    charges: [{ id: 'h0000000-0000-4000-8000-000000000001', order_id: ORDER_ACCEPTED, kind: 'shipping', label: 'Post', amount_minor: 500, position: 0 }],
    payments: [],
    events: [
      { id: 1, order_id: ORDER_DRAFT, at: '2026-10-19T09:00:00Z', by: BROWSER, kind: 'order.created', detail: {} },
      { id: 2, order_id: ORDER_ACCEPTED, at: '2026-10-18T12:00:00Z', by: BROWSER, kind: 'order.created', detail: {} },
      { id: 3, order_id: ORDER_ACCEPTED, at: '2026-10-18T13:00:00Z', by: BROWSER, kind: 'order.status', detail: { from_status: 'quoted', to: 'accepted' } },
    ],
    numbers: { '2026': 3 },
    filaments: [
      filament(FILAMENT_PLA, 'PLA', '#F9D72C', 'Sunflower Yellow', '25.5'),
      filament(FILAMENT_PETG, 'PETG', '#1E1E1E', 'Black', '30'),
    ],
    attachments: [],
    files: {},
  }
}

export let state: State = fixtures()

export function reset(): void {
  state = fixtures()
  counter = 0
}

/** `GET /orders` filters and the pages' tests seed from here. */
export function seedOrders(next: Partial<State>): void {
  state = { ...state, ...next }
}
