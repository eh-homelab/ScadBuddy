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
 * The listing filament for each colour slot of a render (`StoreRender.colors`), by slot:
 * the one whose colour the slot has. A slot no filament matches is left out, and the shop
 * picks it.
 */
export function slotFilaments(colors: readonly string[] | null | undefined, filaments: readonly Filament[]): Record<string, string> {
  const chosen: Record<string, string> = {}
  ;(colors ?? []).forEach((colour, slot) => {
    const filament = filamentOf(filaments, colour)
    if (filament) chosen[String(slot)] = filament.id
  })
  return chosen
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


/** A support material: the shop's choice, never the customer's (owner rule, 2026-11-13). */
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
