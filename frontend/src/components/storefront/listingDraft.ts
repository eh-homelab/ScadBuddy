import type { CustomizerSchema, Filament, ListingBound, MediaView, Param, ParamValue, StorefrontListing, StorefrontListingInput } from '../../api/types'
import type { Listing as PublicListing, PublicParam } from '../../storefront/api/client'

/**
 * #5282 (spec 2026-11-02 §5.3) — a listing as the Storefront page edits it, and what the
 * page makes of it: the body it saves, and the listing the live preview shows.
 *
 * Each parameter is either offered (its bounds in `params`) or locked (its value in
 * `fixed`). A `// color` parameter that is offered takes the colours of the listing's
 * filaments, so its choices are written when the listing is saved, never edited by hand.
 */
export interface ListingDraft {
  slug: string
  commit: string
  title: string
  description: string
  params: Record<string, ListingBound>
  fixed: Record<string, ParamValue>
  colours: string[]
  media_ids: string[]
}

/** The schema's own hidden group: never shown to a customer, so never offered. */
export const HIDDEN_GROUP = 'Hidden'
/** The longest text a customer may type (`orders/storefront/wire.py` `MAX_TEXT_LENGTH`). */
export const MAX_TEXT_LENGTH = 200
const NUMBERS: ReadonlySet<string> = new Set(['number', 'integer', 'slider'])

export function emptyDraft(slug = '', commit = ''): ListingDraft {
  return { slug, commit, title: '', description: '', params: {}, fixed: {}, colours: [], media_ids: [] }
}

export function draftOf(listing: StorefrontListing): ListingDraft {
  return {
    slug: listing.slug,
    commit: listing.commit,
    title: listing.title,
    description: listing.description,
    params: listing.params,
    fixed: listing.fixed,
    colours: listing.colours,
    media_ids: listing.media_ids,
  }
}

/** The parameters a listing can name: every one outside the hidden group. */
export function shownParams(schema: CustomizerSchema | undefined): Param[] {
  return (schema?.parameters ?? []).filter((param) => param.group !== HIDDEN_GROUP)
}

/** Whether a customer can be offered `param` at all: a font or a file never is. */
export function offerable(param: Param): boolean {
  return param.type !== 'font' && param.type !== 'file'
}

/** The bound a parameter is offered with at first: the whole of what its schema allows. */
export function schemaBound(param: Param): ListingBound {
  if (NUMBERS.has(param.type)) {
    const initial = typeof param.initial === 'number' ? param.initial : 0
    return { kind: 'number', min: param.min ?? initial, max: param.max ?? initial, step: param.step ?? null }
  }
  if (param.type === 'boolean') return { kind: 'choice', choices: [true, false] }
  if (param.type === 'select' || param.type === 'color') {
    return { kind: 'choice', choices: (param.options ?? []).map((option) => option.value) }
  }
  return { kind: 'text', max_length: Math.min(param.max_length ?? MAX_TEXT_LENGTH, MAX_TEXT_LENGTH) }
}

/** Whether `a` and `b` are one colour, as publish compares them: case aside. */
function sameColour(a: ParamValue | null | undefined, b: ParamValue | null | undefined): boolean {
  return typeof a === 'string' && typeof b === 'string' && a.toLowerCase() === b.toLowerCase()
}

/** The first of `filaments` whose colour is `colour`. */
function filamentOf(colour: ParamValue | null | undefined, filaments: readonly Filament[]): Filament | undefined {
  return filaments.find((filament) => sameColour(filament.colour, colour))
}

/**
 * #6131 (design 2026-11-13 §3) — a listing's parameters and filaments from the commit's
 * schema and the values the person was looking at (none for a blank listing): free text
 * is the customer's, the rest locked at its value. A colour is offered when its value is
 * the template's default and a filament has it: a customer who picks none gets the
 * default (`orders/storefront/inputs.py` `listing_values`), so a colour the person changed
 * stays locked and sells as seen. Every colour's filament joins the listing, since
 * publish refuses a colour that is not a listing filament's.
 * `test-fixtures/storefront/sell-draft-vectors.json` pins these rules, so another copy of
 * them (the assistant's tool, design §6) can be checked against the same cases.
 */
export function defaultsFor(
  schema: CustomizerSchema,
  filaments: readonly Filament[],
  values: Readonly<Record<string, ParamValue>> = {},
): Pick<ListingDraft, 'params' | 'fixed' | 'colours'> {
  const params: Record<string, ListingBound> = {}
  const fixed: Record<string, ParamValue> = {}
  const colours: string[] = []
  for (const param of shownParams(schema)) {
    const value = values[param.name] ?? param.initial ?? undefined
    if (param.type === 'string' && !(param.options ?? []).length) {
      params[param.name] = schemaBound(param)
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
    if (value !== undefined) fixed[param.name] = value
  }
  return { params, fixed, colours }
}

/** A colour of the listing that none of its filaments has: publish would refuse it. */
export interface ColourProblem {
  name: string
  label: string
  colour: string
}

/**
 * #6131 — every colour publish would refuse (422 `listing_colour_unknown`): a locked
 * colour, or an offered one's default, that none of the listing's filaments has.
 */
export function colourProblems(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): ColourProblem[] {
  const listed = filaments.filter((filament) => draft.colours.includes(filament.id))
  return shownParams(schema).flatMap((param) => {
    if (param.type !== 'color') return []
    const value = draft.params[param.name] ? param.initial : (draft.fixed[param.name] ?? param.initial)
    if (typeof value !== 'string' || filamentOf(value, listed)) return []
    return [{ name: param.name, label: param.caption || param.name, colour: value }]
  })
}

/** `draft` with `filament` on the listing and the colour `name` locked at it. */
export function fixColour(draft: ListingDraft, name: string, filament: Filament): ListingDraft {
  const { [name]: _offered, ...params } = draft.params
  return {
    ...draft,
    params,
    fixed: { ...draft.fixed, [name]: filament.colour },
    colours: draft.colours.includes(filament.id) ? draft.colours : [...draft.colours, filament.id],
  }
}

function rgb(colour: string): [number, number, number] | null {
  const hex = /^#?([0-9a-f]{6})$/i.exec(colour.trim())?.[1]
  if (!hex) return null
  return [parseInt(hex.slice(0, 2), 16), parseInt(hex.slice(2, 4), 16), parseInt(hex.slice(4, 6), 16)]
}

/** The filaments nearest `colour` first (by RGB distance); one whose colour is not `#rrggbb` last. */
export function nearestFilaments(colour: string, filaments: readonly Filament[]): Filament[] {
  const target = rgb(colour)
  const distance = (filament: Filament) => {
    const other = rgb(filament.colour)
    return target && other ? Math.hypot(target[0] - other[0], target[1] - other[1], target[2] - other[2]) : Infinity
  }
  return [...filaments].sort((a, b) => distance(a) - distance(b))
}

function wordPattern(word: string): RegExp {
  return new RegExp(`(?<![\\p{L}\\p{N}])${word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?![\\p{L}\\p{N}])`, 'iu')
}

/**
 * #6131 — the names of the colours a customer can choose that `title` holds (whole words,
 * case aside). The owner's rule: a colour the customer picks never goes in a listing's
 * title ("Owl Pencil Cup – Navy" was refused). Only while the listing offers a colour; a
 * locked colour is the product's own and may stay.
 */
export function titleColours(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): string[] {
  if (!shownParams(schema).some((param) => param.type === 'color' && draft.params[param.name])) return []
  const names = filaments.flatMap((filament) => (draft.colours.includes(filament.id) && filament.colour_name ? [filament.colour_name] : []))
  return [...new Set(names)].filter((name) => wordPattern(name).test(draft.title))
}

/** `title` without each of `words` (whole words, case aside), and without the separators they leave. */
export function withoutWords(title: string, words: readonly string[]): string {
  let next = title
  for (const word of words) next = next.replace(new RegExp(wordPattern(word).source, 'giu'), '')
  return next
    .replace(/\(\s*\)|\[\s*\]/g, '')
    .replace(/\s*([,–—-])\s*(?=[,–—-]|$)/g, '')
    .replace(/^\s*[,–—-]\s*/, '')
    .replace(/\s+,/g, ',')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/**
 * #6131 — the model media a customer can see: images only. A file kept as another type
 * (a hand-uploaded `.glb` is stored as `application/octet-stream`) never shows.
 */
export function sellMedia(media: readonly MediaView[]): string[] {
  return media.filter((item) => item.kind === 'image' && !item.missing && item.content_type.startsWith('image/')).map((item) => item.id)
}

/** The value a locked parameter has: the listing's, else the schema's default. */
export function lockedValue(draft: ListingDraft, param: Param): ParamValue | undefined {
  return draft.fixed[param.name] ?? param.initial ?? undefined
}

function filamentColours(draft: ListingDraft, filaments: readonly Filament[]): string[] {
  return draft.colours.flatMap((id) => filaments.find((filament) => filament.id === id)?.colour ?? [])
}

/** The body the page saves: each offered colour parameter takes the filaments' colours. */
export function listingInput(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): StorefrontListingInput {
  const colours = filamentColours(draft, filaments)
  const types = new Map((schema?.parameters ?? []).map((param) => [param.name, param.type]))
  const params = Object.fromEntries(
    Object.entries(draft.params).map(([name, bound]) => [name, types.get(name) === 'color' ? { kind: 'choice' as const, choices: colours } : bound]),
  )
  return { ...draft, params }
}

/** `value` as JSON with every object's keys sorted, so two equal bodies compare equal whatever order the server wrote their keys in. */
function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    item !== null && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(Object.entries(item as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
      : item,
  )
}

/** What a saved body is compared by, to tell whether a draft needs saving. */
export function savedKey(body: StorefrontListingInput): string {
  return canonical(body)
}

/** The stored listing's body, as `savedKey` compares it. */
export function storedKey(listing: StorefrontListing): string {
  return canonical(draftOf(listing))
}

/**
 * Why `body` cannot be saved, or null: a parameter offered with nothing to choose (most
 * often a colour parameter with every filament unchecked), which the backend refuses as
 * 422 `listing_choice_empty`.
 */
export function unsaveable(body: StorefrontListingInput): string | null {
  for (const [name, bound] of Object.entries(body.params ?? {})) {
    if (bound.kind === 'choice' && bound.choices.length === 0) {
      return `'${name}' is offered with nothing to choose; for a colour parameter, choose at least one filament, or lock it`
    }
  }
  return null
}

/** The listing as a visitor would see it now, for the live preview. */
export function previewListing(
  draft: ListingDraft,
  schema: CustomizerSchema | undefined,
  filaments: readonly Filament[],
  media: readonly MediaView[],
  mediaUrl: (item: MediaView) => string,
): PublicListing {
  const input = listingInput(draft, schema, filaments)
  const params: Record<string, PublicParam> = {}
  for (const param of schema?.parameters ?? []) {
    const bound = input.params?.[param.name]
    if (!bound) continue
    params[param.name] = { ...bound, label: param.caption || param.name, group: param.group ?? '', colour: param.type === 'color' } as PublicParam
  }
  return {
    id: 'preview',
    title: draft.title || 'Untitled listing',
    description: draft.description,
    params,
    colours: draft.colours.flatMap((id) => {
      const filament = filaments.find((f) => f.id === id)
      return filament ? [{ id, colour: filament.colour, colour_name: filament.colour_name, material: filament.material }] : []
    }),
    media: draft.media_ids.flatMap((id) => {
      const item = media.find((m) => m.id === id)
      return item ? [{ url: mediaUrl(item), kind: item.kind, caption: item.caption }] : []
    }),
    updated_at: '',
  }
}
