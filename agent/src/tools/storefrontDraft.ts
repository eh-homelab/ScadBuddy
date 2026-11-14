import type { components } from '../api/schema.js'

// #6131 (design 2026-11-13 §4) — the smart defaults of a storefront listing, the agent's
// copy of the Storefront page's (`frontend/src/components/storefront/listingDraft.ts`
// `defaultsFor`, `colourProblems`, `titleColours`, `withoutWords`, `sellMedia`). The two
// packages share no code, so `test-fixtures/storefront/sell-draft-vectors.json` holds the
// cases both tests check, and a change to one copy must keep them passing in both.

type Schema = components['schemas']['CustomizerSchema']
type Parameter = components['schemas']['Parameter']
type Filament = components['schemas']['Filament']
type MediaView = components['schemas']['MediaView']
type Bound = components['schemas']['ListingInput']['params'] extends Record<string, infer B> | undefined ? B : never
type Value = boolean | number | string

/** The schema's own hidden group: never shown to a customer, so never offered. */
const HIDDEN_GROUP = 'Hidden'
/** The longest text a customer may type (`orders/storefront/wire.py` `MAX_TEXT_LENGTH`). */
const MAX_TEXT_LENGTH = 200

export interface ListingDefaults {
  params: Record<string, Bound>
  fixed: Record<string, Value>
  colours: string[]
}

function shownParams(schema: Schema): Parameter[] {
  return (schema.parameters ?? []).filter((param) => param.group !== HIDDEN_GROUP)
}

/**
 * The owner's rule: support material and settings are internal, never shown to a customer,
 * even when the template exposes them (the owner, 2026-11-13). A parameter whose name, caption or
 * group says "support" or "interface" is one.
 */
export function isSupport(param: Parameter): boolean {
  return [param.name, param.caption, param.group].some((text) => typeof text === 'string' && /support|interface/i.test(text))
}

function sameColour(a: unknown, b: unknown): boolean {
  return typeof a === 'string' && typeof b === 'string' && a.toLowerCase() === b.toLowerCase()
}

function filamentOf(colour: unknown, filaments: readonly Filament[]): Filament | undefined {
  return filaments.find((filament) => sameColour(filament.colour, colour))
}

/**
 * The filaments a listing offers at first: every price-book filament with a density (one
 * that can be priced by weight) of the design's material, which is the material of the
 * first filament whose colour one of the colour values is; any material when none is.
 * Those the colour values name come first, in the schema's order.
 */
export function offeredFilaments(colourValues: readonly unknown[], filaments: readonly Filament[]): Filament[] {
  const named = colourValues.flatMap((value) => filamentOf(value, filaments) ?? [])
  const material = named[0]?.material
  const pool = filaments.filter((filament) => filament.density_g_cm3 != null && (material === undefined || filament.material === material))
  const first = named.filter((filament) => pool.includes(filament))
  return [...new Set([...first, ...pool])]
}

/**
 * The listing's parameters and filaments from the commit's schema and the values: free text
 * (at most the schema's `max_length`, capped at 200) and every colour are the customer's
 * (the owner: "why can't I specify colors"), the rest, support settings always (`isSupport`), is locked at its value. A colour is
 * locked only by the caller, or when the price book has no filament to offer
 * (`offeredFilaments`). A customer who picks no colour gets the listing's default, else the
 * template's (`orders/storefront/inputs.py` `listing_values`, #6144), which must be a listing filament's:
 * `colourProblems` names one that is not.
 */
export function defaultsFor(schema: Schema, filaments: readonly Filament[], values: Readonly<Record<string, Value>> = {}): ListingDefaults {
  const params: Record<string, Bound> = {}
  const fixed: Record<string, Value> = {}
  const shown = shownParams(schema)
  const valueOf = (param: Parameter): Value | undefined => values[param.name] ?? param.initial ?? undefined
  const offered = offeredFilaments(
    shown.filter((param) => param.type === 'color' && !isSupport(param)).flatMap((param) => valueOf(param) ?? []),
    filaments,
  )
  for (const param of shown) {
    const value = valueOf(param)
    if (isSupport(param)) {
      if (value !== undefined) fixed[param.name] = value
    } else if (param.type === 'string' && !(param.options ?? []).length) {
      params[param.name] = { kind: 'text', max_length: Math.min(param.max_length ?? MAX_TEXT_LENGTH, MAX_TEXT_LENGTH) }
    } else if (param.type === 'color' && offered.length > 0) {
      params[param.name] = { kind: 'choice', choices: [] }
    } else if (value !== undefined) {
      fixed[param.name] = value
    }
  }
  // A locked support colour needs its filament on the listing, or publish refuses it
  // (`listing_colour_unknown`); one the price book has exactly, with a density so the estimate
  // can price it, is added, as the page does. Any other stays a `colourProblems` entry.
  const priced = filaments.filter((filament) => filament.density_g_cm3 != null)
  const support = shown
    .filter((param) => param.type === 'color' && isSupport(param))
    .flatMap((param) => filamentOf(valueOf(param), priced) ?? [])
    .filter((filament) => !offered.includes(filament))
  return { params, fixed, colours: [...offered, ...new Set(support)].map((filament) => filament.id) }
}

/**
 * A colour publish would refuse: a locked colour, or an offered one's default, that no listing
 * filament has. An offered colour's default is the listing's `default`, else the template's, as
 * publish checks it (`effective_default`, #6144).
 */
export interface ColourProblem {
  name: string
  label: string
  colour: string
}

export function colourProblems(listing: ListingDefaults, schema: Schema, filaments: readonly Filament[]): ColourProblem[] {
  const listed = filaments.filter((filament) => listing.colours.includes(filament.id))
  return shownParams(schema).flatMap((param) => {
    if (param.type !== 'color') return []
    const offered = listing.params[param.name]
    const value = offered ? (offered.default ?? param.initial) : (listing.fixed[param.name] ?? param.initial)
    if (typeof value !== 'string' || filamentOf(value, listed)) return []
    return [{ name: param.name, label: param.caption || param.name, colour: value }]
  })
}

/**
 * The body a save sends, as the page's `listingInput` writes it: a support parameter is never
 * offered (`isSupport`), and each offered colour takes the listing filaments' colours but a
 * colour only a support parameter uses.
 */
export function withColourChoices(listing: ListingDefaults, schema: Schema, filaments: readonly Filament[]): Pick<ListingDefaults, 'params' | 'fixed'> {
  const byName = new Map((schema.parameters ?? []).map((param) => [param.name, param]))
  const fixed = { ...listing.fixed }
  const params: Record<string, Bound> = {}
  for (const [name, bound] of Object.entries(listing.params)) {
    const param = byName.get(name)
    if (param && isSupport(param)) {
      const value = param.initial ?? undefined
      if (value !== undefined) fixed[name] = value
    } else {
      params[name] = bound
    }
  }
  const valueOf = (param: Parameter): Value | undefined => (params[param.name] ? param.initial : (fixed[param.name] ?? param.initial)) ?? undefined
  // A listing filament is support-only when only a support setting uses its colour and its
  // material is not the design's, as the page's `withoutSupport` decides it.
  const colourParams = (schema.parameters ?? []).filter((param) => param.type === 'color')
  const customerValues = colourParams.filter((param) => !isSupport(param)).map(valueOf)
  const supportValues = colourParams
    .filter(isSupport)
    .map(valueOf)
    .filter((colour) => !customerValues.some((value) => sameColour(value, colour)))
  const listed = listing.colours.flatMap((id) => filaments.find((filament) => filament.id === id) ?? [])
  const supportColoured = (filament: Filament) => supportValues.some((colour) => sameColour(colour, filament.colour))
  const named = listed.filter((filament) => customerValues.some((value) => sameColour(value, filament.colour)))
  const materials = new Set((named.length > 0 ? named : listed.filter((filament) => !supportColoured(filament))).map((filament) => filament.material))
  const supportOnlyFilaments = listed.filter((filament) => supportColoured(filament) && !materials.has(filament.material))
  const supportOnly = supportValues.filter((colour) => {
    const same = listed.filter((filament) => sameColour(filament.colour, colour))
    return same.length > 0 && same.every((filament) => supportOnlyFilaments.includes(filament))
  })
  const colours = listed.map((filament) => filament.colour).filter((colour) => !supportOnly.some((support) => sameColour(support, colour)))
  for (const name of Object.keys(params)) {
    if (byName.get(name)?.type === 'color') params[name] = { kind: 'choice', choices: colours }
  }
  return { params, fixed }
}

function wordPattern(word: string, flags: string): RegExp {
  return new RegExp(`(?<![\\p{L}\\p{N}])${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\p{L}\\p{N}])`, flags)
}

/**
 * The owner's rule: a colour the customer can choose never goes in a listing's title. The
 * names of the colours the saved body offers (`withColourChoices`' choices, as the page's
 * `titleColours` reads `listingInput`) that `title` holds: a support-only filament's colour,
 * which no customer can choose, stays.
 */
export function titleColours(title: string, listing: ListingDefaults, schema: Schema, filaments: readonly Filament[]): string[] {
  const offered = Object.values(withColourChoices(listing, schema, filaments).params).flatMap((bound) => (bound.kind === 'choice' ? bound.choices : []))
  if (offered.length === 0) return []
  const names = filaments.flatMap((filament) =>
    listing.colours.includes(filament.id) && filament.colour_name && offered.some((colour) => sameColour(colour, filament.colour)) ? [filament.colour_name] : [],
  )
  return [...new Set(names)].filter((name) => wordPattern(name, 'iu').test(title))
}

/** `title` without each of `words` (whole words, case aside), and without the separators they leave. */
export function withoutWords(title: string, words: readonly string[]): string {
  let next = title
  for (const word of words) next = next.replace(wordPattern(word, 'giu'), '')
  return next
    .replace(/\(\s*\)|\[\s*\]/g, '')
    .replace(/[,–—/:|·-]\s*(?=[,–—/:|·-])/g, '')
    .replace(/\s*[,–—/:|·-]\s*$/, '')
    .replace(/^\s*[,–—/:|·-]\s*/, '')
    .replace(/\s+,/g, ',')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/** The model media a customer can see: images only (a revision's `.glb` is kept as octet-stream). */
export function sellMedia(media: readonly MediaView[]): string[] {
  return media.filter((item) => item.kind === 'image' && !item.missing && item.content_type.startsWith('image/')).map((item) => item.id)
}
