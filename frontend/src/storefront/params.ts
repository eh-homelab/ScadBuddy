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

/**
 * #6152 — `picks` with one filament per colour. A slot of the render shows only a colour,
 * so it prints from one filament, and the shop refuses two filaments of one colour (422
 * `pick_colour_clash`, `inputs.py` `checked_picks`). The pick of `first` wins, else the
 * earliest; each other pick of that colour moves to its filament. `moved` names them.
 */
export function oneFilamentPerColour(
  listing: Listing,
  picks: Readonly<Record<string, string>>,
  first?: string,
): { picks: Record<string, string>; moved: string[] } {
  const byId = new Map(listing.colours.map((filament) => [filament.id, filament]))
  const names = Object.keys(picks)
  const order = first !== undefined && first in picks ? [first, ...names.filter((name) => name !== first)] : names
  const byColour = new Map<string, string>()
  const next = { ...picks }
  const moved: string[] = []
  for (const name of order) {
    const filament = byId.get(picks[name]!)
    if (!filament) continue
    const colour = filament.colour.toLowerCase()
    const held = byColour.get(colour)
    if (held === undefined) byColour.set(colour, filament.id)
    else if (held !== filament.id) {
      next[name] = held
      moved.push(name)
    }
  }
  return { picks: next, moved }
}

/** The materials a customer may pick on `listing`, in the shop's order (#6152). */
export function listingMaterials(listing: Listing): string[] {
  return [...new Set(customerFilaments(listing.colours).map((filament) => filament.material))]
}

/** The colour an unpicked parameter starts at: the listing's default once it has one (#6144), else its first choice. */
export function startColour(param: PublicParam): string | undefined {
  const start = (param as { default?: unknown }).default
  if (typeof start === 'string') return start
  return param.kind === 'choice' ? param.choices.find((c): c is string => typeof c === 'string') : undefined
}

/**
 * #6152 — on a `one_material` listing, the material shown first: the first that has a
 * filament of every colour parameter's start colour, else the listing's first.
 */
export function startMaterial(listing: Listing): string | null {
  const materials = listingMaterials(listing)
  const colours = Object.values(listing.params).filter((param) => param.kind === 'choice' && param.colour)
  return (
    materials.find((material) =>
      colours.every((param) => {
        const start = startColour(param)?.toLowerCase()
        return paramFilaments(param, listing.colours, material).some((filament) => filament.colour.toLowerCase() === start)
      }),
    ) ??
    materials[0] ??
    null
  )
}

/**
 * #6152 — on a `one_material` listing, every colour parameter set to a filament of
 * `material`, so the page never sends a colour of another one (the CRUD side refuses
 * that, 422 `listing_mixed_material`): a pick of `material` stays; any other colour, set
 * or not, moves to the nearest (RGB distance) of `material`. `moved` is whether a colour
 * the visitor set changed. Any other listing is returned as it is.
 */
export function settleMaterial(
  listing: Listing,
  values: Readonly<Record<string, ParamValue>>,
  picks: Readonly<Record<string, string>>,
  material: string | null,
): { values: Record<string, ParamValue>; picks: Record<string, string>; moved: boolean } {
  const nextValues = { ...values }
  const nextPicks = { ...picks }
  let moved = false
  if (!listing.one_material || material === null) return { values: nextValues, picks: nextPicks, moved }
  const keptNames = new Set<string>()
  for (const [name, param] of Object.entries(listing.params)) {
    if (!(param.kind === 'choice' && param.colour)) continue
    const offered = paramFilaments(param, listing.colours, material)
    const set = values[name]
    const kept = offered.find((filament) => filament.id === picks[name] && typeof set === 'string' && filament.colour.toLowerCase() === set.toLowerCase())
    if (kept) {
      keptNames.add(name)
      continue
    }
    const from = typeof set === 'string' ? set : startColour(param)
    const nearest = from === undefined ? offered[0] : nearestFilament(from, offered)
    if (nearest) {
      nextValues[name] = nearest.colour
      nextPicks[name] = nearest.id
      if (typeof set === 'string' && (nearest.colour.toLowerCase() !== set.toLowerCase() || (picks[name] !== undefined && nearest.id !== picks[name]))) moved = true
    } else {
      delete nextValues[name]
      delete nextPicks[name]
      if (typeof set === 'string') moved = true
    }
  }
  // #6152: one filament per colour (`oneFilamentPerColour`). A pick the visitor kept wins
  // over one this function chose; the colours stay as they are.
  const kept = Object.keys(nextPicks).filter((name) => keptNames.has(name))
  const ordered = Object.fromEntries([...kept, ...Object.keys(nextPicks).filter((name) => !keptNames.has(name))].map((name) => [name, nextPicks[name]!]))
  return { values: nextValues, picks: oneFilamentPerColour(listing, ordered).picks, moved }
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

/** A filament's name with its finish: "Blue · Matte"; the hex when it has no name. */
function finishedName(filament: Filament): string {
  const name = filament.colour_name?.trim() || filament.colour.toUpperCase()
  const subtype = filament.subtype?.trim()
  return subtype && !name.toLowerCase().includes(subtype.toLowerCase()) ? `${name} · ${subtype}` : name
}

/**
 * What the customer reads for each of `filaments`, by id: its colour's name and finish
 * (else its hex). Two of one such name and material are told apart by their brand, else
 * by their hex (#6152).
 */
export function filamentLabels(filaments: readonly Filament[]): Map<string, string> {
  const key = (filament: Filament, extra = '') => `${finishedName(filament).toLowerCase()}\u0000${filament.material.toLowerCase()}\u0000${extra}`
  const count = (keys: string[]) => keys.reduce((seen, k) => seen.set(k, (seen.get(k) ?? 0) + 1), new Map<string, number>())
  const byName = count(filaments.map((filament) => key(filament)))
  const byBrand = count(filaments.map((filament) => key(filament, filament.brand?.trim().toLowerCase() ?? '')))
  return new Map(
    filaments.map((filament) => {
      const name = finishedName(filament)
      if ((byName.get(key(filament)) ?? 0) < 2 || !filament.colour_name) return [filament.id, name]
      const brand = filament.brand?.trim()
      if (brand && (byBrand.get(key(filament, brand.toLowerCase())) ?? 0) < 2) return [filament.id, `${name} (${brand})`]
      return [filament.id, `${name} ${filament.colour.toUpperCase()}`]
    }),
  )
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
