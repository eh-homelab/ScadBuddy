import type { ShopMoney } from '../../components/filament/purchasing/useMoney'
import { divDecimal, fromScaled, mulDecimals, parseDecimal, toScaled } from '../../lib/decimal'
import { formatUnitCost } from '../../lib/money'
import type { Offer, SettableStatus, Tracking, Unit, UnitStatus } from './api'

/** What each tracking means to a person. */
export const TRACKING_LABELS: Record<Tracking, string> = { lot: 'lots', unit: 'units', filament: 'filament', none: 'not counted' }

/** A decimal string from the API; the routes write `null` only for a figure they never hold. */
export const dec = (value: string | null | undefined) => value ?? '0'

/** A quantity with its unit: "200", "1.5 g" (`each` is left out). */
export function qtyText(value: string | null | undefined, uom: string): string {
  const text = dec(value)
  return uom === 'each' ? text : `${text} ${uom}`
}

/**
 * A cost in minor units of the shop currency, with its fraction. An unknown cost reads
 * "unknown", never 0.00 (R-P2-35); with the currency not yet read, the bare minor figure.
 */
export function minorText(minor: string | null | undefined, money: ShopMoney, unknown = false): string {
  if (unknown) return 'unknown'
  if (!money.currency) return dec(minor)
  return formatUnitCost(dec(minor), money.currency, money.exponent)
}

/** A currency's minor-unit digits as `Intl` knows them (ISO 4217: JPY 0, USD 2, KWD 3), or null for a code it refuses. */
export function currencyExponent(code: string): number | null {
  try {
    return new Intl.NumberFormat('en', { style: 'currency', currency: code }).resolvedOptions().maximumFractionDigits ?? null
  } catch {
    return null
  }
}

/**
 * A lot's cost in the currency it was bought in (`foreign_unit_cost_minor`, minor units of
 * `currency`), at that currency's own digits; the raw minor figure only for a code with none known.
 */
export function foreignCostText(minor: string, currency: string, money: ShopMoney): string {
  const exponent = currency === money.currency ? money.exponent : currencyExponent(currency)
  if (exponent === null) return `${minor} minor units of ${currency}`
  return formatUnitCost(minor, currency, exponent)
}

/** A stock value: the known part, and "unknown" for what has no cost (R-P2-35). */
export function valueText(minor: string | null | undefined, unknown: boolean, money: ShopMoney): string {
  if (!unknown) return minorText(minor, money)
  return Number(dec(minor)) === 0 ? 'unknown' : `${minorText(minor, money)} and some unknown`
}

/** `price × qty / packSize` in major units, rounded to the currency's digits (Ruling 4's prefill). */
export function packCost(price: string, qty: string, packSize: string, exponent: number): string {
  return divDecimal(mulDecimals(price, qty), packSize, exponent)
}

/**
 * A typed major amount as minor units (a decimal string), or null when it is not a number of
 * at most `places` decimals (`exponent` by default; more for a unit cost, kept as a fraction).
 */
export function majorToMinor(text: string, exponent: number, places = exponent): string | null {
  const parsed = parseDecimal(text)
  if (parsed === null) return null
  const fraction = parsed.split('.')[1]?.length ?? 0
  if (fraction > places) return null
  if (fraction <= exponent) return toScaled(parsed, exponent).toString()
  return fromScaled(toScaled(parsed, fraction), fraction - exponent)
}

/** A minor-unit decimal as the major amount to type: "599" at 2 is "5.99", "5.99" is "0.0599". */
export function minorToMajor(minor: string, exponent: number): string {
  const scale = minor.split('.')[1]?.length ?? 0
  const text = fromScaled(toScaled(minor, scale), scale + exponent)
  return text.includes('.') ? text.replace(/0+$/, '').replace(/\.$/, '') : text
}

/** A date-time as the viewer's day. */
export const dayText = (at: string) => new Date(at).toLocaleDateString()

export const STATUS_LABELS: Record<UnitStatus, string> = { in_stock: 'In stock', in_service: 'In service', retired: 'Retired', scrapped: 'Scrapped', sold: 'Sold', ended: 'Ended' }
/** The statuses a person can choose, as `STATUS_LABELS` orders them. */
export const SETTABLE_STATUSES = (Object.keys(STATUS_LABELS) as UnitStatus[]).filter((status): status is SettableStatus => status !== 'ended')

/** The newest offer price in the shop's currency: what "Receive" prefills from (Ruling 4). */
export function pricedOffer(offers: Offer[], currency: string | null): Offer | undefined {
  return offers
    .filter((offer) => offer.latest_price && offer.latest_price.currency === currency)
    .sort((a, b) => b.latest_price!.on_date.localeCompare(a.latest_price!.on_date))[0]
}

/** A unit's name in a list: its serial, else its name, else "Unit" and its place. */
export function unitLabel(unit: Unit, at: number): string {
  return unit.serial || unit.name || `Unit ${at + 1}`
}

/** A service life: "500 print hours", "12 months", or both; "not set" for neither. */
export function lifeText(hours: string | null, months: number | null): string {
  const parts = [hours !== null ? `${hours} print hours` : null, months !== null ? `${months} ${months === 1 ? 'month' : 'months'}` : null].filter(Boolean)
  return parts.length ? parts.join(' or ') : 'not set'
}
