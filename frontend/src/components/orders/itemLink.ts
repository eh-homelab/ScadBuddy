/**
 * #4693 — a line item's element id on the order page, the row `/orders/<id>?item=<item id>`
 * (or `#item-<item id>`) scrolls to and opens.
 */
export function itemAnchor(itemId: string): string {
  return `item-${itemId}`
}
