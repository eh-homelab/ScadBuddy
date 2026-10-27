const ORDER_NUMBER = /^ORD-\d{4}-\d+$/

/** A pattern that finds one order number (`ORD-<yyyy>-<n>`, backend `orders/numbers.py`)
 *  inside longer text, as a whole token: ORD-2026-3 never matches ORD-2026-30 or
 *  ORD-2026-3-a. It takes only an order number and throws on anything else, so the text
 *  it puts into the pattern holds no metacharacters and needs no escaping, and a negative
 *  check (`queryBy…(…)).toBeNull()`) cannot be weakened by free text. Where the whole
 *  name is the number (a heading, a link), match the string exactly instead.
 *  (#4752, #4762, #4768, #4778, #4782, #4786.) */
export function orderNumberMatcher(number: string): RegExp {
  if (!ORDER_NUMBER.test(number)) throw new Error(`Not an order number: ${JSON.stringify(number)}`)
  return new RegExp(`(?<![\\w-])${number}(?![\\w-])`)
}
