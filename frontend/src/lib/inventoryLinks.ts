/**
 * #7067 — every inventory page's path, from one place, as `filamentLinks.ts` does for the
 * Filament section. The paths are relative router paths. A lot has no page of its own: its
 * path opens its item's page at the lot's row (`/inventory/lots/:id` redirects there), and an
 * offer is a row of its item's page. `test-fixtures/inventory/ui-paths.json` pins these; the
 * agent's copy (layer 5) is tested against the same vectors.
 */

const BASE = '/inventory'
const at = (list: string, id: string) => `${list}/${encodeURIComponent(id)}`
const anchored = (path: string, anchor: string) => `${path}#${encodeURIComponent(anchor)}`

/** The anchor ids the item page gives a lot row and an offer row. */
export const inventoryAnchors = {
  lot: (lotId: string) => `lot-${lotId}`,
  offer: (offerId: string) => `offer-${offerId}`,
}

const item = (id: string) => at(`${BASE}/items`, id)

/** The kinds whose lot items, with the kinds below them, are an order's parts (R-P2-39).
 * Inventory K1
 * put both under `consumable`: `part` holds durable units now. */
export const PART_KINDS = ['hardware', 'ams_part'] as const

/** The kind the Parts views open. The list takes one kind, so they open `hardware`, the kind of
 * every part the old `/parts` routes made; `ams_part` is next to it in the kind tree. */
export const PARTS_KIND = PART_KINDS[0]

/** Whether the kind is one of `PART_KINDS` or below one; `parentOf` gives a kind's parent. */
export function isPartKind(kindId: string, parentOf: (id: string) => string | null | undefined): boolean {
  const seen = new Set<string>()
  for (let at: string | null | undefined = kindId; at && !seen.has(at); at = parentOf(at)) {
    if ((PART_KINDS as readonly string[]).includes(at)) return true
    seen.add(at)
  }
  return false
}

export const inventoryPaths = {
  list: BASE,
  /** The items of a kind and of its descendants (the list's `kind` filter). */
  kind: (kindId: string) => `${BASE}?${new URLSearchParams({ kind: kindId }).toString()}`,
  kinds: `${BASE}/kinds`,
  item,
  unit: (id: string) => at(`${BASE}/units`, id),
  /** A lot's own path: the router sends it to its item's page, at the lot's row. */
  lot: (id: string) => at(`${BASE}/lots`, id),
  /** A lot's row on its item's page. */
  itemLot: (itemId: string, lotId: string) => anchored(item(itemId), inventoryAnchors.lot(lotId)),
  /** A supplier offer's row on its item's page. */
  offer: (itemId: string, offerId: string) => anchored(item(itemId), inventoryAnchors.offer(offerId)),
} as const
