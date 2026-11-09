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

/** Whether a new listing offers `param`: a ranged number, a select, a switch or free text. */
function offeredAtFirst(param: Param): boolean {
  if (NUMBERS.has(param.type)) return param.min != null && param.max != null
  return param.type === 'select' || param.type === 'boolean' || param.type === 'string'
}

/** A new listing's parameters, from the commit's schema. */
export function defaultsFor(schema: CustomizerSchema): Pick<ListingDraft, 'params' | 'fixed'> {
  const params: Record<string, ListingBound> = {}
  const fixed: Record<string, ParamValue> = {}
  for (const param of shownParams(schema)) {
    if (offeredAtFirst(param)) params[param.name] = schemaBound(param)
    else if (param.initial != null) fixed[param.name] = param.initial
  }
  return { params, fixed }
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
