/**
 * #7067 — the Inventory area's client for `/api/v1/inventory/*` (`backend/scadbuddy/api/
 * inventory.py`, `inventory_stock.py`), typed from the generated `schema.d.ts`. A quantity or
 * a cost is a decimal string on the wire; a cost is in minor units of the shop currency
 * (`orders_currency`), and an offer's pack price is a major amount in its own currency.
 *
 * #6949 K1 — a kind, item, lot, unit or move write runs as scadbuddy-core's activity: it goes
 * through `command`, with an `Idempotency-Key`, and a slow one answers 202 with an operation,
 * followed at `GET /inventory/operations/{id}` to its result or its problem. `onAccepted`
 * hears that 202; a press while the write runs passes the same `key`, so it attaches.
 */
import { ApiError, command, request, stockWrite } from '../../api/client'
import type { components } from '../../api/schema'
import type { OperationAccepted } from '../../api/types'

type Schemas = components['schemas']

export type ItemKind = Schemas['ItemKind']
export type KindInput = Schemas['KindInput']
export type KindPatch = Schemas['KindPatch']
export type KindSchema = Schemas['KindSchema']
export type Item = Schemas['Item']
export type ItemInput = Schemas['ItemInput']
export type ItemPatch = Schemas['InventoryItemPatch']
export type Offer = Schemas['Offer']
export type OfferInput = Schemas['OfferInput']
export type OfferPricePoint = Schemas['OfferPricePoint']
export type PriceInput = Schemas['PriceInput']
export type Lot = Schemas['Lot']
export type LotPatch = Schemas['LotPatch']
export type ReceiveLot = Schemas['ReceiveLot']
export type ReceiveUnits = Schemas['ReceiveUnits']
export type Unit = Schemas['Unit']
export type UnitPatch = Schemas['UnitPatch']
export type UnitStatus = Unit['status']
export type UnitStatusInput = Schemas['UnitStatusInput']
/** A status a person can set: `ended` is a status only a unit's life sets. */
export type SettableStatus = UnitStatusInput['status']
export type UseInput = Schemas['UseInput']
export type CountInput = Schemas['CountInput']
export type Move = Schemas['Move']
export type RecostResult = Schemas['RecostResult']
export type Tracking = ItemKind['tracking']

const BASE = '/inventory'
const seg = encodeURIComponent

function query(params: Record<string, string | number | boolean | null | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) if (value != null && value !== '' && value !== false) search.set(key, String(value))
  const text = search.toString()
  return text ? `?${text}` : ''
}

const json = (method: string, body: unknown): RequestInit => ({ method, body: JSON.stringify(body) })

/** The press of one write: its key (kept while it runs) and who hears its 202. */
export interface WriteOptions {
  key?: string
  onAccepted?: (operation: OperationAccepted) => void
}

/**
 * The `type` of the problem a write throws when its operation succeeded but no longer holds
 * the answer (Go prunes a `crud_requests` row after 7 days): the change was made.
 */
export const RESULT_GONE = 'urn:scadbuddy:inventory-result-gone'

async function write<T>(path: string, init: RequestInit, { key, onAccepted }: WriteOptions = {}): Promise<T> {
  const answer = await command<T | null | undefined>(path, init, `${BASE}/operations`, key, onAccepted)
  if (answer == null) {
    throw new ApiError({
      type: RESULT_GONE,
      title: 'Gone',
      status: 410,
      detail: 'The change was saved, but ScadBuddy no longer has its answer. Reload the page to see it.',
    })
  }
  return answer
}
const post: RequestInit = { method: 'POST' }

export interface ItemFilter {
  kind?: string | null
  q?: string | null
  low?: boolean
  archived?: boolean
  /** 1 to 5000; the route answers 1000 when absent. */
  limit?: number
}

export const inventoryApi = {
  kinds: (archived = false) => request<{ kinds: ItemKind[] }>(`${BASE}/kinds${query({ archived })}`).then((body) => body.kinds),
  createKind: (body: KindInput, o?: WriteOptions) => write<ItemKind>(`${BASE}/kinds`, json('POST', body), o),
  /** Only what is sent changes; a bad `attribute_schema` is 422 `bad_property_schema`. */
  patchKind: (id: string, body: KindPatch, o?: WriteOptions) => write<ItemKind>(`${BASE}/kinds/${seg(id)}`, json('PATCH', body), o),
  archiveKind: (id: string, o?: WriteOptions) => write<ItemKind>(`${BASE}/kinds/${seg(id)}/archive`, post, o),
  restoreKind: (id: string, o?: WriteOptions) => write<ItemKind>(`${BASE}/kinds/${seg(id)}/restore`, post, o),
  kindSchema: (id: string) => request<KindSchema>(`${BASE}/kinds/${seg(id)}/schema`),

  /** `kind` takes the kind with its descendants. */
  items: (filter: ItemFilter = {}) =>
    request<{ items: Item[] }>(`${BASE}/items${query({ kind: filter.kind, q: filter.q?.trim(), low: filter.low, archived: filter.archived, limit: filter.limit })}`).then((body) => body.items),
  item: (id: string) => request<Item>(`${BASE}/items/${seg(id)}`),
  createItem: (body: ItemInput, o?: WriteOptions) => write<Item>(`${BASE}/items`, json('POST', body), o),
  patchItem: (id: string, body: ItemPatch, o?: WriteOptions) => write<Item>(`${BASE}/items/${seg(id)}`, json('PATCH', body), o),
  archiveItem: (id: string, o?: WriteOptions) => write<Item>(`${BASE}/items/${seg(id)}/archive`, post, o),
  restoreItem: (id: string, o?: WriteOptions) => write<Item>(`${BASE}/items/${seg(id)}/restore`, post, o),

  itemOffers: (itemId: string) => request<{ offers: Offer[] }>(`${BASE}/items/${seg(itemId)}/offers`).then((body) => body.offers),
  supplierOffers: (supplierId: string) => request<{ offers: Offer[] }>(`${BASE}/suppliers/${seg(supplierId)}/offers`).then((body) => body.offers),
  createOffer: (body: OfferInput) => request<Offer>(`${BASE}/offers`, json('POST', body)),
  archiveOffer: (id: string) => request<Offer>(`${BASE}/offers/${seg(id)}/archive`, { method: 'POST' }),
  /** A pack price, `manual`; the shop's currency and today when absent. */
  addPrice: (offerId: string, body: PriceInput) => request<OfferPricePoint>(`${BASE}/offers/${seg(offerId)}/prices`, json('POST', body)),

  lots: (itemId: string) => request<{ lots: Lot[] }>(`${BASE}/items/${seg(itemId)}/lots`).then((body) => body.lots),
  lot: (id: string) => request<Lot>(`${BASE}/lots/${seg(id)}`),
  receiveLot: (itemId: string, body: ReceiveLot, o?: WriteOptions) => write<Lot>(`${BASE}/items/${seg(itemId)}/lots`, json('POST', body), o),
  /** A cost set here moves no past move until the lot is re-costed. */
  patchLot: (id: string, body: LotPatch, o?: WriteOptions) => write<Lot>(`${BASE}/lots/${seg(id)}`, json('PATCH', body), o),
  /** A 202 is the `RecostLot` workflow still running (#6949 K1 Task 16). */
  recostLot: (id: string, key?: string) => stockWrite<RecostResult>(`${BASE}/lots/${seg(id)}/recost`, { method: 'POST' }, key),
  use: (itemId: string, body: UseInput, o?: WriteOptions) => write<{ moves: Move[] }>(`${BASE}/items/${seg(itemId)}/use`, json('POST', body), o).then((b) => b.moves),
  count: (itemId: string, body: CountInput, o?: WriteOptions) => write<{ moves: Move[] }>(`${BASE}/items/${seg(itemId)}/count`, json('POST', body), o).then((b) => b.moves),

  units: (itemId: string) => request<{ units: Unit[] }>(`${BASE}/items/${seg(itemId)}/units`).then((body) => body.units),
  unit: (id: string) => request<Unit>(`${BASE}/units/${seg(id)}`),
  receiveUnits: (itemId: string, body: ReceiveUnits, o?: WriteOptions) => write<{ units: Unit[] }>(`${BASE}/items/${seg(itemId)}/units`, json('POST', body), o).then((b) => b.units),
  patchUnit: (id: string, body: UnitPatch, o?: WriteOptions) => write<Unit>(`${BASE}/units/${seg(id)}`, json('PATCH', body), o),
  setUnitStatus: (id: string, body: UnitStatusInput, o?: WriteOptions) => write<Unit>(`${BASE}/units/${seg(id)}/status`, json('POST', body), o),

  moves: (filter: { item_id?: string; lot_id?: string; unit_id?: string; limit?: number }) => request<{ moves: Move[] }>(`${BASE}/moves${query(filter)}`).then((body) => body.moves),
}
