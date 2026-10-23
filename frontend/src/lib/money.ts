/**
 * #4249, #4250 — money on an order is integer minor units in the order's currency (spec
 * 2026-10-18 §3.6). How many decimal places a currency's minor unit has is the backend's
 * one table (ISO 4217, `backend/scadbuddy/orders/currency.py`), published as
 * `minor_exponent` on every order and on the price book, and passed in here: `Intl`'s own
 * digits are CLDR's, which differ from ISO 4217's for several codes (IQD is 0 there and 3
 * here), so the browser never asks `Intl` for them (plan 2026-10-22 Ruling 5). Setting
 * both `minimumFractionDigits` and `maximumFractionDigits` overrides the currency style's
 * own digits (https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Intl/NumberFormat/NumberFormat#maximumfractiondigits).
 */

export function formatMoney(minor: number, currency: string, exponent: number): string {
  const major = minor / 10 ** exponent
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      minimumFractionDigits: exponent,
      maximumFractionDigits: exponent,
    }).format(major)
  } catch {
    return `${major.toFixed(exponent)} ${currency}`
  }
}

/**
 * The minor units a typed major amount ("12.34") means at `exponent` decimal places, or
 * null for anything else, a finer amount included ("12.345" at 2): never rounded.
 */
export function toMinor(major: string, exponent: number): number | null {
  const text = major.trim()
  const match = /^(-?)(\d+)(?:\.(\d+))?$/.exec(text)
  if (!match) return null
  const [, sign, whole, fraction = ''] = match
  if (fraction.length > exponent) return null
  const minor = Number(`${whole}${fraction.padEnd(exponent, '0')}`)
  return sign === '-' && minor !== 0 ? -minor : minor
}

/** The major amount to type for `minor`, as a plain decimal string. */
export function toMajor(minor: number, exponent: number): string {
  return (minor / 10 ** exponent).toFixed(exponent)
}
