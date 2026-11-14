import type { Filament, Listing, ParamValue, PublicParam } from './api/client'

/** How far a number may sit from a step, to a float's rounding (`inputs.py` `_STEP_TOLERANCE`). */
const STEP_TOLERANCE = 1e-9

/** A plain decimal, as typed: digits, one optional point, an optional leading minus. */
const DECIMAL = /^-?(?:\d+\.?\d*|\.\d+)$/

/**
 * The number `text` means, or NaN for anything but a plain decimal: never hex ("0x10"),
 * an exponent or "Infinity", which `Number()` would take while the box still shows them.
 */
export function parseNumber(text: string): number {
  const trimmed = text.trim()
  return DECIMAL.test(trimmed) ? Number(trimmed) : Number.NaN
}

/** A value as the customer reads it: a switch is "Yes" or "No", never "true" or "false" (#6141). */
export function valueText(value: ParamValue): string {
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  return String(value)
}

/** The code points of `text`: the backend counts a name's length so, not in UTF-16 units. */
export function textLength(text: string): number {
  return Array.from(text).length
}

/**
 * Why the listing refuses `value` for `param`, in the visitor's words, or null. A value the
 * visitor did not set (undefined) is the shop's own, never refused here.
 */
export function valueProblem(param: PublicParam, value: ParamValue | undefined): string | null {
  if (value === undefined) return null
  if (param.kind === 'number') {
    const range = `Choose from ${param.min} to ${param.max}`
    const rule = param.step ? `${range}, in steps of ${param.step}.` : `${range}.`
    if (typeof value !== 'number' || !Number.isFinite(value) || value < param.min || value > param.max) return rule
    if (param.step) {
      const times = (value - param.min) / param.step
      if (Math.abs(times - Math.round(times)) > STEP_TOLERANCE) return rule
    }
    return null
  }
  if (param.kind === 'text') {
    return typeof value === 'string' && textLength(value) <= param.max_length ? null : `Use at most ${param.max_length} characters.`
  }
  return param.choices.some((choice) => choice === value) ? null : 'Pick one of the choices.'
}

/** Whether a colour of `listing` has no filament left (every one archived): it cannot be ordered. */
export function colourGone(param: PublicParam): boolean {
  return param.kind === 'choice' && param.colour && param.choices.length === 0
}

export function listingUnavailable(listing: Listing): boolean {
  return Object.values(listing.params).some(colourGone)
}

/** The listing filament whose colour is `colour`, compared as the backend does (casefolded). */
export function filamentOf(filaments: readonly Filament[], colour: string): Filament | undefined {
  const wanted = colour.toLowerCase()
  return filaments.find((filament) => filament.colour.toLowerCase() === wanted)
}

export function filamentName(filament: Filament): string {
  return filament.colour_name ? `${filament.colour_name} ${filament.material}` : `${filament.material} ${filament.colour}`
}

/**
 * #6152 — of `filaments`, those a customer may pick for colour parameter `param`: its
 * choices' colours, never a support material, of `material` only when one is set.
 */
export function paramFilaments(param: PublicParam, filaments: readonly Filament[], material: string | null = null): Filament[] {
  if (param.kind !== 'choice') return []
  const colours = new Set(param.choices.filter((c): c is string => typeof c === 'string').map((c) => c.toLowerCase()))
  return customerFilaments(filaments).filter(
    (filament) =>
      colours.has(filament.colour.toLowerCase()) && (material === null || filament.material.toLowerCase() === material.toLowerCase()),
  )
}

function rgb(hex: string): [number, number, number] {
  const value = Number.parseInt(hex.replace('#', '').slice(0, 6), 16)
  return [(value >> 16) & 255, (value >> 8) & 255, value & 255]
}

/** Of `filaments`, the one whose colour is nearest `colour` (RGB distance), or undefined for none. */
export function nearestFilament(colour: string, filaments: readonly Filament[]): Filament | undefined {
  const [r, g, b] = rgb(colour)
  let best: Filament | undefined
  let bestDistance = Number.POSITIVE_INFINITY
  for (const filament of filaments) {
    const [fr, fg, fb] = rgb(filament.colour)
    const distance = (r - fr) ** 2 + (g - fg) ** 2 + (b - fb) ** 2
    if (distance < bestDistance) {
      best = filament
      bestDistance = distance
    }
  }
  return best
}

/**
 * #6152 — of `picks` (parameter name to filament id), those `listing` still allows: an
 * offered colour parameter, a filament it offers, of the colour in `values`. The shop's
 * side refuses any other (`inputs.py` `checked_picks`).
 */
export function keepPicks(
  listing: Listing,
  values: Readonly<Record<string, ParamValue>>,
  picks: Readonly<Record<string, string>>,
): Record<string, string> {
  const kept: Record<string, string> = {}
  for (const [name, id] of Object.entries(picks)) {
    const param = listing.params[name]
    const value = values[name]
    if (!param || typeof value !== 'string') continue
    const filament = paramFilaments(param, listing.colours).find((f) => f.id === id)
    if (filament && filament.colour.toLowerCase() === value.toLowerCase()) kept[name] = id
  }
  return kept
}

/** The materials a customer may pick on `listing`, in the shop's order (#6152). */
export function listingMaterials(listing: Listing): string[] {
  return [...new Set(customerFilaments(listing.colours).map((filament) => filament.material))]
}

/** Of `values`, those `listing` still offers and allows: what survives a reload of it. */
export function keepOffered(listing: Listing, values: Readonly<Record<string, ParamValue>>): Record<string, ParamValue> {
  const kept: Record<string, ParamValue> = {}
  for (const [name, value] of Object.entries(values)) {
    const param = listing.params[name]
    if (param && valueProblem(param, value) === null) kept[name] = value
  }
  return kept
}

/** A colour's extruder note, "(extruder 2)": the shop's detail, never the customer's. */
const EXTRUDER_NOTE = /\s*\(\s*extruder\s*[0-9]+\s*\)/gi

/**
 * What the customer reads for parameter `name`: the listing's own `label`, else the
 * template's `caption` without its extruder note, else the name (#6137). The back office's
 * preview uses it; the public page reads the label the backend made by the same rule
 * (`backend/scadbuddy/orders/storefront/wire.py` `shown_label`): change both together.
 */
export function shownLabel(name: string, label: string | null | undefined, caption: string | null | undefined): string {
  if (label) return label
  if (caption) return caption.replace(EXTRUDER_NOTE, ' ').split(/\s+/).filter(Boolean).join(' ') || caption
  return name
}


/**
 * A support material: the shop's choice, never the customer's (owner rule, 2026-11-13).
 * The shop's copy is `backend/scadbuddy/orders/storefront/wire.py` `SUPPORT_MATERIAL`:
 * change both together.
 */
const SUPPORT_MATERIAL = /support|pva|bvoh|hips/i

/** Of `filaments`, those a customer may pick: never a support material. */
export function customerFilaments(filaments: readonly Filament[]): Filament[] {
  return filaments.filter((filament) => !SUPPORT_MATERIAL.test(filament.material))
}

/**
 * What the customer reads for each of `filaments`, by id: its colour's name (else its
 * hex). Two of one name and material are told apart by their hex (#6152).
 */
export function filamentLabels(filaments: readonly Filament[]): Map<string, string> {
  const name = (filament: Filament) => filament.colour_name?.trim() || filament.colour.toUpperCase()
  const seen = new Map<string, number>()
  for (const filament of filaments) {
    const key = `${name(filament).toLowerCase()}\u0000${filament.material.toLowerCase()}`
    seen.set(key, (seen.get(key) ?? 0) + 1)
  }
  return new Map(
    filaments.map((filament) => {
      const key = `${name(filament).toLowerCase()}\u0000${filament.material.toLowerCase()}`
      const shared = (seen.get(key) ?? 0) > 1 && filament.colour_name
      return [filament.id, shared ? `${name(filament)} ${filament.colour.toUpperCase()}` : name(filament)]
    }),
  )
}
