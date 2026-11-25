/**
 * #7067 — the Inventory area's client for `/api/v1/inventory/*` (`backend/scadbuddy/api/
 * inventory.py`, `inventory_stock.py`), typed from the generated `schema.d.ts`. A quantity or
 * a cost is a decimal string on the wire; a cost is in minor units of the shop currency
 * (`orders_currency`), and an offer's pack price is a major amount in its own currency.
 */
import { request } from '../../api/client'
import type { components } from '../../api/schema'

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

export interface ItemFilter {
  kind?: string | null
  q?: string | null
  low?: boolean
  archived?: boolean
}

export const inventoryApi = {
  kinds: (archived = false) => request<{ kinds: ItemKind[] }>(`${BASE}/kinds${query({ archived })}`).then((body) => body.kinds),
  createKind: (body: KindInput) => request<ItemKind>(`${BASE}/kinds`, json('POST', body)),
  /** Only what is sent changes; a bad `attribute_schema` is 422 `bad_property_schema`. */
  patchKind: (id: string, body: KindPatch) => request<ItemKind>(`${BASE}/kinds/${seg(id)}`, json('PATCH', body)),
  archiveKind: (id: string) => request<ItemKind>(`${BASE}/kinds/${seg(id)}/archive`, { method: 'POST' }),
  restoreKind: (id: string) => request<ItemKind>(`${BASE}/kinds/${seg(id)}/restore`, { method: 'POST' }),
  kindSchema: (id: string) => request<KindSchema>(`${BASE}/kinds/${seg(id)}/schema`),

  /** `kind` takes the kind with its descendants. */
  items: (filter: ItemFilter = {}) =>
    request<{ items: Item[] }>(`${BASE}/items${query({ kind: filter.kind, q: filter.q?.trim(), low: filter.low, archived: filter.archived })}`).then((body) => body.items),
  item: (id: string) => request<Item>(`${BASE}/items/${seg(id)}`),
  createItem: (body: ItemInput) => request<Item>(`${BASE}/items`, json('POST', body)),
  patchItem: (id: string, body: ItemPatch) => request<Item>(`${BASE}/items/${seg(id)}`, json('PATCH', body)),
  archiveItem: (id: string) => request<Item>(`${BASE}/items/${seg(id)}/archive`, { method: 'POST' }),
  restoreItem: (id: string) => request<Item>(`${BASE}/items/${seg(id)}/restore`, { method: 'POST' }),

  itemOffers: (itemId: string) => request<{ offers: Offer[] }>(`${BASE}/items/${seg(itemId)}/offers`).then((body) => body.offers),
  supplierOffers: (supplierId: string) => request<{ offers: Offer[] }>(`${BASE}/suppliers/${seg(supplierId)}/offers`).then((body) => body.offers),
  createOffer: (body: OfferInput) => request<Offer>(`${BASE}/offers`, json('POST', body)),
  archiveOffer: (id: string) => request<Offer>(`${BASE}/offers/${seg(id)}/archive`, { method: 'POST' }),
  /** A pack price, `manual`; the shop's currency and today when absent. */
  addPrice: (offerId: string, body: PriceInput) => request<OfferPricePoint>(`${BASE}/offers/${seg(offerId)}/prices`, json('POST', body)),

  lots: (itemId: string) => request<{ lots: Lot[] }>(`${BASE}/items/${seg(itemId)}/lots`).then((body) => body.lots),
  lot: (id: string) => request<Lot>(`${BASE}/lots/${seg(id)}`),
  receiveLot: (itemId: string, body: ReceiveLot) => request<Lot>(`${BASE}/items/${seg(itemId)}/lots`, json('POST', body)),
  /** A cost set here moves no past move until the lot is re-costed. */
  patchLot: (id: string, body: LotPatch) => request<Lot>(`${BASE}/lots/${seg(id)}`, json('PATCH', body)),
  recostLot: (id: string) => request<RecostResult>(`${BASE}/lots/${seg(id)}/recost`, { method: 'POST' }),
  use: (itemId: string, body: UseInput) => request<{ moves: Move[] }>(`${BASE}/items/${seg(itemId)}/use`, json('POST', body)).then((b) => b.moves),
  count: (itemId: string, body: CountInput) => request<{ moves: Move[] }>(`${BASE}/items/${seg(itemId)}/count`, json('POST', body)).then((b) => b.moves),

  units: (itemId: string) => request<{ units: Unit[] }>(`${BASE}/items/${seg(itemId)}/units`).then((body) => body.units),
  unit: (id: string) => request<Unit>(`${BASE}/units/${seg(id)}`),
  receiveUnits: (itemId: string, body: ReceiveUnits) => request<{ units: Unit[] }>(`${BASE}/items/${seg(itemId)}/units`, json('POST', body)).then((b) => b.units),
  patchUnit: (id: string, body: UnitPatch) => request<Unit>(`${BASE}/units/${seg(id)}`, json('PATCH', body)),
  setUnitStatus: (id: string, body: UnitStatusInput) => request<Unit>(`${BASE}/units/${seg(id)}/status`, json('POST', body)),

  moves: (filter: { item_id?: string; lot_id?: string; unit_id?: string; limit?: number }) => request<{ moves: Move[] }>(`${BASE}/moves${query(filter)}`).then((body) => body.moves),
}
