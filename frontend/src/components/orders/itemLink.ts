/**
 * #4693 — a line item's element id on the order page, the row `/orders/<id>?item=<item id>`
 * (or `#item-<item id>`) scrolls to and opens.
 */
export function itemAnchor(itemId: string): string {
  return `item-${itemId}`
}

/** #4954 — the custom-item entry row's title input: "Add custom item" moves the focus here. */
export const CUSTOM_ITEM_TITLE = 'custom-item-title'
