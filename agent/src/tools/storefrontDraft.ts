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

function sameColour(a: unknown, b: unknown): boolean {
  return typeof a === 'string' && typeof b === 'string' && a.toLowerCase() === b.toLowerCase()
}

function filamentOf(colour: unknown, filaments: readonly Filament[]): Filament | undefined {
  return filaments.find((filament) => sameColour(filament.colour, colour))
}

/**
 * The listing's parameters and filaments from the commit's schema and the values: free text
 * is the customer's (at most the schema's `max_length`, capped at 200), a colour is offered
 * when its value is the template's default and a filament has it (a customer who picks none
 * gets the default, `orders/storefront/inputs.py` `listing_values`), the rest is locked at its
 * value. Every colour's filament joins the listing: publish refuses a colour that is not a
 * listing filament's.
 */
export function defaultsFor(schema: Schema, filaments: readonly Filament[], values: Readonly<Record<string, Value>> = {}): ListingDefaults {
  const params: Record<string, Bound> = {}
  const fixed: Record<string, Value> = {}
  const colours: string[] = []
  for (const param of shownParams(schema)) {
    const value = values[param.name] ?? param.initial ?? undefined
    if (param.type === 'string' && !(param.options ?? []).length) {
      params[param.name] = { kind: 'text', max_length: Math.min(param.max_length ?? MAX_TEXT_LENGTH, MAX_TEXT_LENGTH) }
      continue
    }
    if (param.type === 'color') {
      const filament = filamentOf(value, filaments)
      if (filament && !colours.includes(filament.id)) colours.push(filament.id)
      if (filament && sameColour(value, param.initial)) {
        params[param.name] = { kind: 'choice', choices: [] }
        continue
      }
    }
    if (value !== undefined && value !== null) fixed[param.name] = value
  }
  return { params, fixed, colours }
}

/** A colour publish would refuse: a locked colour, or an offered one's default, that no listing filament has. */
export interface ColourProblem {
  name: string
  label: string
  colour: string
}

export function colourProblems(listing: ListingDefaults, schema: Schema, filaments: readonly Filament[]): ColourProblem[] {
  const listed = filaments.filter((filament) => listing.colours.includes(filament.id))
  return shownParams(schema).flatMap((param) => {
    if (param.type !== 'color') return []
    const value = listing.params[param.name] ? param.initial : (listing.fixed[param.name] ?? param.initial)
    if (typeof value !== 'string' || filamentOf(value, listed)) return []
    return [{ name: param.name, label: param.caption || param.name, colour: value }]
  })
}

/**
 * The body a save sends: each offered colour takes the listing filaments' colours, as the
 * page's `listingInput` writes them.
 */
export function withColourChoices(listing: ListingDefaults, schema: Schema, filaments: readonly Filament[]): Record<string, Bound> {
  const colours = listing.colours.flatMap((id) => filaments.find((filament) => filament.id === id)?.colour ?? [])
  const types = new Map((schema.parameters ?? []).map((param) => [param.name, param.type]))
  return Object.fromEntries(Object.entries(listing.params).map(([name, bound]) => [name, types.get(name) === 'color' ? { kind: 'choice' as const, choices: colours } : bound]))
}

function wordPattern(word: string, flags: string): RegExp {
  return new RegExp(`(?<![\\p{L}\\p{N}])${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\p{L}\\p{N}])`, flags)
}

/**
 * The owner's rule: a colour the customer can choose never goes in a listing's title. The
 * names of the listing filaments' colours that `title` holds while the listing offers a colour.
 */
export function titleColours(title: string, listing: ListingDefaults, schema: Schema, filaments: readonly Filament[]): string[] {
  if (!shownParams(schema).some((param) => param.type === 'color' && listing.params[param.name])) return []
  const names = filaments.flatMap((filament) => (listing.colours.includes(filament.id) && filament.colour_name ? [filament.colour_name] : []))
  return [...new Set(names)].filter((name) => wordPattern(name, 'iu').test(title))
}

/** `title` without each of `words` (whole words, case aside), and without the separators they leave. */
export function withoutWords(title: string, words: readonly string[]): string {
  let next = title
  for (const word of words) next = next.replace(wordPattern(word, 'giu'), '')
  return next
    .replace(/\(\s*\)|\[\s*\]/g, '')
    .replace(/\s*([,–—-])\s*(?=[,–—-]|$)/g, '')
    .replace(/^\s*[,–—-]\s*/, '')
    .replace(/\s+,/g, ',')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/** The model media a customer can see: images only (a revision's `.glb` is kept as octet-stream). */
export function sellMedia(media: readonly MediaView[]): string[] {
  return media.filter((item) => item.kind === 'image' && !item.missing && item.content_type.startsWith('image/')).map((item) => item.id)
}
