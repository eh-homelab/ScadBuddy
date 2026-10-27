/** A line item's largest quantity, the API's bound (`api/orders.py` `MAX_QUANTITY`, #4763). */
export const MAX_QUANTITY = 2 ** 31 - 1

/** What to show when `parseQuantity` refuses the text. */
export const QUANTITY_HINT = 'Enter how many to make, a whole number of 1 or more.'

/**
 * A typed quantity as a whole number from 1 to `MAX_QUANTITY`, or null. Digits only, so
 * text such as `1e9` or `2.0`, which `Number` reads as an integer, is refused.
 */
export function parseQuantity(text: string): number | null {
  const trimmed = text.trim()
  if (!/^\d+$/.test(trimmed)) return null
  const count = Number(trimmed)
  return count >= 1 && count <= MAX_QUANTITY ? count : null
}
