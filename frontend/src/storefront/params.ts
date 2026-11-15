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

/**
 * Whether `listing` cannot be ordered: the shop says no material can print it (`available`
 * false, #6302 review: its locked colours share no material with the offered ones), or a
 * colour has no filament left.
 */
export function listingUnavailable(listing: Listing): boolean {
  return listing.available === false || Object.values(listing.params).some(colourGone)
}

/**
 * #6152 — of `filaments`, those a customer may pick for colour parameter `param`: its
 * choices' colours, never a support material.
 */
export function paramFilaments(param: PublicParam, filaments: readonly Filament[]): Filament[] {
  if (param.kind !== 'choice') return []
  const colours = new Set(param.choices.filter((c): c is string => typeof c === 'string').map((c) => c.toLowerCase()))
  return customerFilaments(filaments).filter((filament) => colours.has(filament.colour.toLowerCase()))
}

/**
 * The colour an unpicked parameter starts at: `default`, what an unpicked render takes
 * (the listing's default, else the template's, `inputs.py` `effective_default`, #6144).
 * None when the page may not show it (its filament is gone).
 */
export function startColour(param: PublicParam): string | undefined {
  return typeof param.default === 'string' ? param.default : undefined
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
 * The shop's copy is `backend/scadbuddy/orders/storefront/wire.py` `SUPPORT_MATERIAL`, and
 * the agent's `agent/src/tools/storefrontDraft.ts` `SUPPORT_MATERIAL`: change all together.
 */
const SUPPORT_MATERIAL = /support|pva|bvoh|hips/i

/** Of `filaments`, those a customer may pick: never a support material. */
export function customerFilaments<T extends { material: string }>(filaments: readonly T[]): T[] {
  return filaments.filter((filament) => !SUPPORT_MATERIAL.test(filament.material))
}

/** How a filament looks beyond its hex, from its finish and name (#6152). */
export type Finish = 'translucent' | 'silk' | 'multi' | 'solid'

export function filamentFinish(filament: Filament): Finish {
  const text = `${filament.subtype ?? ''} ${filament.colour_name ?? ''}`
  if (/rainbow|tri.?colou?r|dual|multi|gradient/i.test(text)) return 'multi'
  if (/translucent|transparent|clear/i.test(text)) return 'translucent'
  if (/silk|metallic|shimmer/i.test(text)) return 'silk'
  return 'solid'
}

/** The swatch's CSS background for `filament`: its hex, drawn as its finish looks. */
export function swatchBackground(filament: Filament): string {
  const colour = filament.colour
  switch (filamentFinish(filament)) {
    case 'translucent':
      return `linear-gradient(${colour}99, ${colour}99), repeating-conic-gradient(#ccc 0 25%, #fff 0 50%) 50% / 10px 10px`
    case 'silk':
      return `linear-gradient(135deg, ${colour} 30%, #ffffffb3 50%, ${colour} 70%)`
    case 'multi':
      return `conic-gradient(${colour}, #e8532f, #f2c14e, #1c8c8c, #1b6ca8, ${colour})`
    default:
      return colour
  }
}
