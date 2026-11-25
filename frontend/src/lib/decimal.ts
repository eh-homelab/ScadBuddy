/**
 * #6266 — exact decimal strings for money. The backend sends and takes a money amount as
 * a decimal string (`"21.4"`), never a float; this arithmetic works on those strings with
 * `BigInt` over the longest scale, so `0.1 + 0.2` is `0.3` and nothing goes through
 * `Number`. Only `formatCost` hands a string to `Intl`, which reads its digits exactly
 * (https://developer.mozilla.org/en-US/docs/Web/JavaScript/Reference/Global_Objects/Intl/NumberFormat/format).
 */

export type Decimal = string

const PLAIN = /^(-?)(\d+)(?:\.(\d+))?$/

/** The canonical dot form of typed text (`12`, `12.5`, `12,5`), or `null` for anything else. */
export function parseDecimal(text: string): string | null {
  const match = /^(\d+)(?:[.,](\d+))?$/.exec(text.trim())
  if (!match) return null
  return match[2] === undefined ? match[1]! : `${match[1]}.${match[2]}`
}

function split(value: string): { sign: bigint; whole: string; fraction: string } {
  const match = PLAIN.exec(value)
  if (!match) throw new RangeError(`not a decimal: ${value}`)
  return { sign: match[1] ? -1n : 1n, whole: match[2]!, fraction: match[3] ?? '' }
}

/** `value` as an integer count of 10^-scale; `scale` is at least its own digits. */
export function toScaled(value: string, scale: number): bigint {
  const { sign, whole, fraction } = split(value)
  return sign * BigInt(whole + fraction.padEnd(scale, '0'))
}

/** The decimal string of an integer count of 10^-scale. */
export function fromScaled(count: bigint, scale: number): string {
  const negative = count < 0n
  const digits = (negative ? -count : count).toString().padStart(scale + 1, '0')
  const text = scale === 0 ? digits : `${digits.slice(0, -scale)}.${digits.slice(-scale)}`
  return negative ? `-${text}` : text
}

const scaleOf = (value: string) => split(value).fraction.length

function binary(a: string, b: string, op: (x: bigint, y: bigint) => bigint): string {
  const scale = Math.max(scaleOf(a), scaleOf(b))
  return fromScaled(op(toScaled(a, scale), toScaled(b, scale)), scale)
}

export const addDecimals = (a: string, b: string) => binary(a, b, (x, y) => x + y)
export const subDecimals = (a: string, b: string) => binary(a, b, (x, y) => x - y)

export function mulDecimal(a: string, intFactor: number): string {
  if (!Number.isInteger(intFactor)) throw new RangeError('the factor is a whole number')
  const scale = scaleOf(a)
  return fromScaled(toScaled(a, scale) * BigInt(intFactor), scale)
}

/** `a × b` of two decimal strings, exactly, with the places of both (#7067). */
export function mulDecimals(a: string, b: string): string {
  const scaleA = scaleOf(a)
  const scaleB = scaleOf(b)
  return fromScaled(toScaled(a, scaleA) * toScaled(b, scaleB), scaleA + scaleB)
}

/** `value` rounded half up (away from zero) to `places` decimals, with no trailing zeros (#7067). */
export function roundDecimal(value: string, places: number): string {
  const rounded = divDecimal(value, '1', places)
  const text = rounded.includes('.') ? rounded.replace(/0+$/, '').replace(/\.$/, '') : rounded
  return text === '-0' ? '0' : text
}

export function cmpDecimals(a: string, b: string): -1 | 0 | 1 {
  const scale = Math.max(scaleOf(a), scaleOf(b))
  const x = toScaled(a, scale)
  const y = toScaled(b, scale)
  return x < y ? -1 : x > y ? 1 : 0
}

/** `a / b` to `places` decimals, rounded half up (away from zero), as the backend rounds. */
export function divDecimal(a: string, b: string, places: number): string {
  const scale = Math.max(scaleOf(a), scaleOf(b))
  const num = toScaled(a, scale) * 10n ** BigInt(places)
  const den = toScaled(b, scale)
  if (den === 0n) throw new RangeError('division by zero')
  const negative = num < 0n !== den < 0n
  const n = num < 0n ? -num : num
  const d = den < 0n ? -den : den
  const rounded = (2n * n + d) / (2n * d)
  return fromScaled(negative ? -rounded : rounded, places)
}

/**
 * A money amount in the currency, with at least `exponent` decimals and up to two more,
 * the digits the string gives and never `Intl`'s own for the currency (`lib/money.ts`
 * `formatUnitCost`). `format(string)` keeps the string's exact digits only where the engine
 * has `Intl.NumberFormat` v3; an older one reads it as a float, which can round the last
 * digit of a long amount.
 */
export function formatCost(value: string, currency: string, exponent: number): string {
  try {
    return new Intl.NumberFormat(undefined, {
      style: 'currency',
      currency,
      minimumFractionDigits: exponent,
      maximumFractionDigits: exponent + 2,
    }).format(value as `${number}`)
  } catch {
    return `${value} ${currency}`
  }
}

/** A decimal string with grouping and at least two digits, and no currency symbol. */
export function formatDecimal(value: string): string {
  try {
    return new Intl.NumberFormat(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(value as `${number}`)
  } catch {
    return value
  }
}
