import type { components } from '../api/schema.js'

// #6131 (design 2026-11-13 §4) — the smart defaults of a storefront listing, the agent's
// copy of the Storefront page's (`frontend/src/components/storefront/listingDraft.ts`
// `defaultsFor`, `colourProblems`, `titleColours`, `withoutWords`, `sellMedia`, and a library
// file's draft below, #6710). The two
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
/** A support material (`orders/storefront/wire.py` `SUPPORT_MATERIAL`, `frontend/src/storefront/params.ts`): change all together. */
const SUPPORT_MATERIAL = /support|pva|bvoh|hips/i

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
 * #6258, #6302 review: the materials a draft allows, so it can publish even when the design's
 * material is not one the shop's default (`storefront_materials`) allows. They are the
 * materials of the listing's filaments, casefolded, support left out: PETG, then PLA, then the
 * rest by name (the order the backend's migration `20261115T0346Z_listing_materials_backfill.sql`
 * gives existing listings).
 */
export function draftMaterials(colours: readonly string[], filaments: readonly Filament[]): string[] {
  const found = new Map<string, string>()
  for (const id of colours) {
    const material = filaments.find((filament) => filament.id === id)?.material.trim()
    if (material && !SUPPORT_MATERIAL.test(material) && !found.has(material.toLowerCase())) found.set(material.toLowerCase(), material)
  }
  const rank = (folded: string) => (folded === 'petg' ? 0 : folded === 'pla' ? 1 : 2)
  return [...found.keys()].sort((a, b) => rank(a) - rank(b) || (a < b ? -1 : a > b ? 1 : 0)).map((folded) => found.get(folded) as string)
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
  const colours = [...offered, ...new Set(support)].map((filament) => filament.id)
  // #6144: an offered colour whose template default no colour on offer has starts at the value
  // given, else the first colour on offer, so publish takes it (the page's `fittedDefault`).
  const chosen = withColourChoices({ params, fixed, colours }, schema, filaments).params
  for (const param of shown) {
    const bound = chosen[param.name]
    const own = params[param.name]
    if (!own || param.type !== 'color' || bound?.kind !== 'choice') continue
    const pickable = bound.choices
    if (pickable.some((colour) => sameColour(colour, param.initial))) continue
    const value = valueOf(param)
    const start = pickable.find((colour) => sameColour(colour, value)) !== undefined ? value : pickable[0]
    if (start !== undefined) params[param.name] = { ...own, default: start }
  }
  return { params, fixed, colours }
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
 * colour only a support parameter uses and a support material's colour.
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
  // #6152: never the colour of a support material, which publish refuses on offer.
  const colours = listed
    .filter((filament) => !SUPPORT_MATERIAL.test(filament.material))
    .map((filament) => filament.colour)
    .filter((colour) => !supportOnly.some((support) => sameColour(support, colour)))
  for (const name of Object.keys(params)) {
    // A colour's choices are the filaments'; its own label (#6137) and default (#6144) stay.
    const bound = params[name]
    if (byName.get(name)?.type === 'color')
      params[name] = {
        kind: 'choice',
        choices: colours,
        ...(bound?.label ? { label: bound.label } : {}),
        ...(bound?.default != null ? { default: bound.default } : {}),
      }
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

// #6710 (design 2026-11-21 §9.3, §11.3) — a library file's draft, the agent's copy of the
// editor's (`listingDraft.ts` `fileTitle`, `fileColours`, `supportFilament`, `declaredSlots`,
// `platePictures`, and `titleColours` of a file draft), checked against the file case of the
// same shared vectors.

type SlotNeed = components['schemas']['SlotNeed']
type OutputPlate = components['schemas']['OutputPlate']

/** One part of a file a listing sells, as the editor reads it before a publish. */
export interface FileSlot {
  /** From 0. */
  slot: number
  colour: string
  material: string | null
  used: boolean
  support: boolean
}

/** The types a listing can sell (`orders/storefront/file_listings.py` `LISTED_TYPES`). */
export const LISTED_TYPES: readonly string[] = ['3mf', 'stl']

/** Whether `material` is a support material, which a customer never picks. */
export function isSupportMaterial(material: string | null | undefined): boolean {
  return typeof material === 'string' && SUPPORT_MATERIAL.test(material)
}

function customerFilaments(filaments: readonly Filament[]): Filament[] {
  return filaments.filter((filament) => !SUPPORT_MATERIAL.test(filament.material))
}

/**
 * The file's declared slots (`GET /print/library/{id}/filaments` `slots`) as its parts before a
 * publish: every one counted as used, a support material's as support, one with no colour grey.
 */
export function declaredSlots(slots: readonly SlotNeed[]): FileSlot[] {
  return slots.map((slot) => ({
    slot: slot.slot_id - 1,
    colour: slot.colour || '#808080',
    material: slot.material ?? null,
    used: true,
    support: isSupportMaterial(slot.material),
  }))
}

/** The picture ids (`plate:<n>`) of the file's plates that have a picture, in plate order. */
export function platePictures(plates: readonly OutputPlate[]): string[] {
  return plates.filter((plate) => plate.has_thumbnail).map((plate) => `plate:${plate.index}`)
}

/**
 * A file listing's title at first: the file's name without its ending, its underscores read
 * as spaces, and without the names of the colours a customer can pick.
 */
export function fileTitle(filename: string, filaments: readonly Filament[]): string {
  const name = filename.replace(/(\.gcode)?\.[a-z0-9]+$/i, '').replace(/_+/g, ' ')
  const colours = customerFilaments(filaments).flatMap((filament) => (filament.colour_name ? [filament.colour_name] : []))
  return withoutWords(name, colours).slice(0, 200) || name.slice(0, 200)
}

/**
 * The support filament publish locks support part `slot` to: the first of `colours`' support
 * filaments of the part's material, else its first support filament.
 */
function supportFilament(slot: FileSlot, colours: readonly string[], filaments: readonly Filament[]): Filament | undefined {
  const supports = colours.flatMap((id) => filaments.find((filament) => filament.id === id && isSupportMaterial(filament.material)) ?? [])
  const same = supports.filter((filament) => slot.material !== null && filament.material.trim().toLowerCase() === slot.material.trim().toLowerCase())
  return same[0] ?? supports[0]
}

/**
 * The filaments a file listing offers at first: the priced customer filaments of the material
 * whose colour one of the offered parts has (`offeredFilaments`), and, for each support part,
 * the support filament publish would lock it to.
 */
export function fileColours(slots: readonly FileSlot[], filaments: readonly Filament[]): string[] {
  const offered = slots.filter((slot) => slot.used && !slot.support)
  const customer = offeredFilaments(
    offered.map((slot) => slot.colour),
    customerFilaments(filaments),
  )
  const priced = filaments.filter((filament) => filament.density_g_cm3 != null && isSupportMaterial(filament.material))
  const support = slots
    .filter((slot) => slot.used && slot.support)
    .flatMap((slot) => supportFilament(slot, priced.map((filament) => filament.id), priced) ?? [])
  return [...new Set([...customer, ...support].map((filament) => filament.id))]
}

/**
 * The names of the colours a customer can pick on a file listing that `title` holds: every
 * part takes any listing filament's colour but a support material's.
 */
export function fileTitleColours(title: string, colours: readonly string[], filaments: readonly Filament[]): string[] {
  const listed = filaments.filter((filament) => colours.includes(filament.id))
  const offered = customerFilaments(listed).map((filament) => filament.colour)
  const names = listed.flatMap((filament) => (filament.colour_name && offered.some((colour) => sameColour(colour, filament.colour)) ? [filament.colour_name] : []))
  return [...new Set(names)].filter((name) => wordPattern(name, 'iu').test(title))
}
