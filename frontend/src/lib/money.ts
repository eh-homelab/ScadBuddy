/**
 * #4249 — money on an order is integer minor units in the order's currency (spec
 * 2026-10-18 §3.6). The minor unit's size comes from `Intl.NumberFormat`'s own
 * `maximumFractionDigits` for the currency (2 for USD, 0 for JPY), so a code the
 * runtime does not know falls back to 2.
 */

export function fractionDigits(currency: string): number {
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency }).resolvedOptions().maximumFractionDigits ?? 2
  } catch {
    return 2
  }
}

export function formatMoney(minor: number, currency: string): string {
  const digits = fractionDigits(currency)
  const major = minor / 10 ** digits
  try {
    return new Intl.NumberFormat(undefined, { style: 'currency', currency }).format(major)
  } catch {
    return `${major.toFixed(digits)} ${currency}`
  }
}

/** The minor units a typed major amount ("12.34") means, or null for anything else. */
export function toMinor(major: string, currency: string): number | null {
  const text = major.trim()
  if (text === '' || !/^-?\d+(\.\d+)?$/.test(text)) return null
  return Math.round(Number(text) * 10 ** fractionDigits(currency))
}

/** The major amount to type for `minor`, as a plain decimal string. */
export function toMajor(minor: number, currency: string): string {
  const digits = fractionDigits(currency)
  return (minor / 10 ** digits).toFixed(digits)
}
