import type { CustomizerSchema, Filament, ListingBound, MediaView, Param, ParamValue, StorefrontListing, StorefrontListingInput } from '../../api/types'
import type { Listing as PublicListing, PublicParam } from '../../storefront/api/client'
import { customerFilaments, shownLabel, valueProblem } from '../../storefront/params'

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
  /**
   * The materials a print may be made of, first preferred; empty is the shop's default
   * (`storefront_materials`, #6258). The editor's "Materials allowed" sets them.
   */
  materials: string[]
}

/** The schema's own hidden group: never shown to a customer, so never offered. */
export const HIDDEN_GROUP = 'Hidden'
/** The longest text a customer may type (`orders/storefront/wire.py` `MAX_TEXT_LENGTH`). */
export const MAX_TEXT_LENGTH = 200
const NUMBERS: ReadonlySet<string> = new Set(['number', 'integer', 'slider'])

export function emptyDraft(slug = '', commit = ''): ListingDraft {
  return { slug, commit, title: '', description: '', params: {}, fixed: {}, colours: [], media_ids: [], materials: [] }
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
    // A listing stored before #6258 has none: the shop's default.
    materials: (listing.materials as string[] | undefined) ?? [],
  }
}

/** The most materials a list holds (backend `core/settings.py` `MAX_MATERIALS`). */
export const MAX_MATERIALS = 16

/**
 * A material as publish compares it: trimmed, case aside (backend `inputs.py`
 * `material_key`, which casefolds; a price book's materials are plain ASCII names).
 */
export function materialKey(material: string): string {
  return material.trim().toLowerCase()
}

/**
 * #6258 — the materials a print of `draft` may be made of, first preferred: its own, else
 * the shop's default (backend `inputs.py` `effective_materials`). Null while the shop's
 * default is not read yet.
 */
export function effectiveMaterials(draft: ListingDraft, shop: readonly string[] | undefined): string[] | null {
  // A draft stored before #6258 has no `materials`: the shop's default.
  const own = (draft.materials as string[] | undefined) ?? []
  if (own.length > 0) return own
  return shop ? [...shop] : null
}

/**
 * #6258 — the materials of the price book a customer may be given: each once (case aside,
 * the first spelling kept), never a support material (`customerFilaments`), by name.
 */
export function priceBookMaterials(filaments: readonly Filament[]): string[] {
  const found = new Map<string, string>()
  for (const filament of customerFilaments(filaments)) {
    const material = filament.material.trim()
    if (material && !found.has(materialKey(material))) found.set(materialKey(material), material)
  }
  return [...found.values()].sort((a, b) => a.localeCompare(b))
}

/** What publish refused about a listing's materials, shown beside the control it concerns (#6258). */
export type ListingRefusal =
  /** 422 `listing_material_not_allowed`: the filament `filament` is of a material the listing does not allow. */
  | { kind: 'material_not_allowed'; filament: string; detail: string }
  /** 422 `listing_locked_colours_unprintable`: no allowed material has every locked colour (`names`) and a choice of each offered one. */
  | { kind: 'locked_colours_unprintable'; names: string[]; detail: string }
  /**
   * #6144, #6152 — a refusal about one parameter, `name`: 422 `listing_default_invalid` (it
   * starts at a value its bounds do not allow), `listing_colour_unknown` (its colour, or the
   * one it starts at, is no listing filament's) or `listing_support_offered` (it sets support,
   * so it cannot be offered).
   */
  | { kind: 'param'; code: 'default_invalid' | 'colour_unknown' | 'support_offered'; name: string; detail: string }
  /** 422 `listing_colour_unknown` naming no parameter: a filament on the listing is no longer in the price book. */
  | { kind: 'filament_unknown'; detail: string }

const PARAM_REFUSALS = ['default_invalid', 'colour_unknown', 'support_offered'] as const

const STOREFRONT_PROBLEMS = 'https://scadbuddy.dev/problems/storefront/'

/** The refusal `problem` is, when it is one of the `ListingRefusal`s; else null. */
export function listingRefusal(problem: { type?: string; detail?: string; title: string; [extension: string]: unknown }): ListingRefusal | null {
  const detail = problem.detail ?? problem.title
  if (problem.type === `${STOREFRONT_PROBLEMS}listing_material_not_allowed` && typeof problem['name'] === 'string') {
    return { kind: 'material_not_allowed', filament: problem['name'], detail }
  }
  if (problem.type === `${STOREFRONT_PROBLEMS}listing_locked_colours_unprintable`) {
    const names = Array.isArray(problem['names']) ? problem['names'].filter((name): name is string => typeof name === 'string') : []
    return { kind: 'locked_colours_unprintable', names, detail }
  }
  const code = PARAM_REFUSALS.find((slug) => problem.type === `${STOREFRONT_PROBLEMS}listing_${slug}`)
  if (code !== undefined && typeof problem['name'] === 'string') return { kind: 'param', code, name: problem['name'], detail }
  if (code === 'colour_unknown') return { kind: 'filament_unknown', detail }
  return null
}

/** What publish refused about parameter `label` (`refusal`), said beside it. */
export function refusalText(refusal: Extract<ListingRefusal, { kind: 'param' }>, label: string): string {
  if (refusal.code === 'default_invalid') return `Not published: “${label}” starts at a value these choices do not allow. Set “Starts at” to one they allow.`
  if (refusal.code === 'colour_unknown') return `Not published: “${label}” uses a colour that no colour on offer has. Pick a colour on offer, or tick a filament of that colour under “Colours on offer”.`
  return `Not published: “${label}” sets support, which stays yours and a customer never sees. Turn off “Customer can change”.`
}

/** The parameters a listing can name: every one outside the hidden group. */
export function shownParams(schema: CustomizerSchema | undefined): Param[] {
  return (schema?.parameters ?? []).filter((param) => param.group !== HIDDEN_GROUP)
}

/** Whether a customer can be offered `param` at all: a font or a file never is. */
export function offerable(param: Param): boolean {
  return param.type !== 'font' && param.type !== 'file'
}

/**
 * The owner's rule (2026-11-13): support material and settings are internal, never shown to
 * a customer, even when the template exposes them. A parameter whose name, caption or group
 * says "support" or "interface" is one. #6152: the listing's own `label` counts too, as
 * publish reads it (backend `listings.py` `_sets_support`).
 */
export function isSupport(param: Param, label?: string | null): boolean {
  return [param.name, param.caption, param.group, label].some((text) => typeof text === 'string' && /support|interface/i.test(text))
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
export function sameColour(a: ParamValue | null | undefined, b: ParamValue | null | undefined): boolean {
  return typeof a === 'string' && typeof b === 'string' && a.toLowerCase() === b.toLowerCase()
}

/** The first of `filaments` whose colour is `colour`. */
function filamentOf(colour: ParamValue | null | undefined, filaments: readonly Filament[]): Filament | undefined {
  return filaments.find((filament) => sameColour(filament.colour, colour))
}

/**
 * #6131 — the filaments a listing offers at first: every price-book filament with a
 * density (one that can be priced by weight) of the design's material, which is the
 * material of the first filament whose colour one of the colour values is; any material
 * when none is. Those the colour values name come first, in the schema's order.
 */
export function offeredFilaments(colourValues: readonly ParamValue[], filaments: readonly Filament[]): Filament[] {
  const named = colourValues.flatMap((value) => filamentOf(value, filaments) ?? [])
  const material = named[0]?.material
  const pool = filaments.filter((filament) => filament.density_g_cm3 != null && (material === undefined || filament.material === material))
  const first = named.filter((filament) => pool.includes(filament))
  return [...new Set([...first, ...pool])]
}

/**
 * #6131 (design 2026-11-13 §4) — a listing's parameters and filaments from the commit's
 * schema and the values the person was looking at (none for a blank listing): free text
 * and every colour are the customer's (the owner: "why can't I specify colors"), the rest,
 * support settings always (`isSupport`), is locked at its value. A colour is locked only by the person, or when the price book has
 * no filament to offer (`offeredFilaments`). A customer who picks no colour gets the
 * listing's default, else the template's (`orders/storefront/inputs.py` `listing_values`,
 * #6144), which must be a listing filament's: `colourProblems` names one that is not.
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
  const shown = shownParams(schema)
  const valueOf = (param: Param) => values[param.name] ?? param.initial ?? undefined
  const offered = offeredFilaments(
    shown.filter((param) => param.type === 'color' && !isSupport(param)).flatMap((param) => valueOf(param) ?? []),
    filaments,
  )
  for (const param of shown) {
    const value = valueOf(param)
    if (!isSupport(param) && ((param.type === 'string' && !(param.options ?? []).length) || (param.type === 'color' && offered.length > 0))) {
      params[param.name] = param.type === 'color' ? { kind: 'choice', choices: [] } : schemaBound(param)
    } else if (value !== undefined) {
      fixed[param.name] = value
    }
  }
  // A locked support colour needs its filament on the listing, or publish refuses it
  // (`listing_colour_unknown`); one the price book has exactly, with a density so the
  // estimate can price it, is added (#6140 review). Any other stays a `colourProblems` entry.
  const priced = filaments.filter((filament) => filament.density_g_cm3 != null)
  const support = shown
    .filter((param) => param.type === 'color' && isSupport(param))
    .flatMap((param) => filamentOf(valueOf(param), priced) ?? [])
    .filter((filament) => !offered.includes(filament))
  const colours = [...offered, ...new Set(support)].map((filament) => filament.id)
  // #6144: an offered colour whose template default no colour on offer has starts at the
  // value the person was looking at, else the first colour on offer, so publish takes it.
  const pickable = pickableColours({ ...emptyDraft(), params, fixed, colours }, schema, filaments)
  for (const param of shown) {
    const bound = params[param.name]
    if (bound && param.type === 'color') params[param.name] = fittedDefault(param, bound, pickable, valueOf(param))
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
 * colour, or an offered one's default, that none of the listing's filaments has. An
 * offered colour's default is the listing's `default`, else the template's, as publish
 * checks it (`effective_default`, #6144).
 */
export function colourProblems(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): ColourProblem[] {
  const listed = filaments.filter((filament) => draft.colours.includes(filament.id))
  return shownParams(schema).flatMap((param) => {
    if (param.type !== 'color') return []
    const offered = draft.params[param.name]
    const value = offered ? (offered.default ?? param.initial) : (draft.fixed[param.name] ?? param.initial)
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
 * title ("Owl Pencil Cup – Navy" was refused). The names are those of the colours the saved
 * body offers (`listingInput`'s choices): every listing filament's colour but a support-only
 * filament's (`withoutSupport`), which no customer can choose.
 */
export function titleColours(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): string[] {
  const offered = Object.values(listingInput(draft, schema, filaments).params ?? {}).flatMap((bound) => (bound.kind === 'choice' ? bound.choices : []))
  if (offered.length === 0) return []
  const names = filaments.flatMap((filament) =>
    draft.colours.includes(filament.id) && filament.colour_name && offered.some((colour) => sameColour(colour, filament.colour)) ? [filament.colour_name] : [],
  )
  return [...new Set(names)].filter((name) => wordPattern(name).test(draft.title))
}

/** `title` without each of `words` (whole words, case aside), and without the separators they leave. */
export function withoutWords(title: string, words: readonly string[]): string {
  let next = title
  for (const word of words) next = next.replace(new RegExp(wordPattern(word).source, 'giu'), '')
  return next
    .replace(/\(\s*\)|\[\s*\]/g, '')
    .replace(/[,–—/:|·-]\s*(?=[,–—/:|·-])/g, '')
    .replace(/\s*[,–—/:|·-]\s*$/, '')
    .replace(/^\s*[,–—/:|·-]\s*/, '')
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

/** How far a number may sit from a step, to a float's rounding (backend `inputs.py` `_STEP_TOLERANCE`). */
const STEP_TOLERANCE = 1e-9

/**
 * #6144 — whether an offered `param` may start at `value` under `bound`, as publish checks
 * a default (backend `inputs.py` `value_reason`): a colour one of `colours` (case aside), a
 * number in the range and on the step from `min`, a choice one of the choices, text no
 * longer than the bound.
 */
export function defaultFits(param: Param, bound: ListingBound, value: ParamValue | null | undefined, colours: readonly string[]): boolean {
  if (value === null || value === undefined) return false
  if (param.type === 'color') return colours.some((colour) => sameColour(colour, value))
  if (bound.kind === 'number') {
    if (typeof value !== 'number' || !Number.isFinite(value) || value < bound.min || value > bound.max) return false
    if (param.type === 'integer' && !Number.isInteger(value)) return false
    const step = bound.step ?? param.step
    if (step != null && step > 0) {
      const times = (value - bound.min) / step
      if (Math.abs(times - Math.round(times)) > STEP_TOLERANCE) return false
    }
    return true
  }
  if (bound.kind === 'text') return typeof value === 'string' && [...value].length <= bound.max_length
  return bound.choices.some((choice) => choice === value)
}

/** The value `bound` allows nearest `value`: a number put in the range and on the step, text cut to the length, else the first choice or colour. */
function nearestAllowed(param: Param, bound: ListingBound, value: ParamValue | null | undefined, colours: readonly string[]): ParamValue | undefined {
  if (param.type === 'color') return colours[0]
  if (bound.kind === 'number') {
    const start = typeof value === 'number' && Number.isFinite(value) ? value : bound.min
    const step = bound.step ?? param.step
    const inRange = Math.min(Math.max(start, bound.min), bound.max)
    if (step == null || step <= 0) return inRange
    const onStep = bound.min + Math.floor((inRange - bound.min) / step + STEP_TOLERANCE) * step
    return Number(onStep.toPrecision(12))
  }
  if (bound.kind === 'text') return typeof value === 'string' ? [...value].slice(0, bound.max_length).join('') : ''
  return bound.choices[0]
}

/**
 * #6144 — `bound` with the default an unpicked render takes set only where it must be: a
 * listing default that fits stays; else none, when the template's fits; else `current` (the
 * value the person was looking at, or the locked one) when it fits; else the value the
 * bound allows nearest it. A bound that allows nothing keeps no default. `colours` are the
 * colours a customer can pick, for a colour parameter.
 */
export function fittedDefault(param: Param, bound: ListingBound, colours: readonly string[], current?: ParamValue | null): ListingBound {
  const { default: own, ...rest } = bound
  if (defaultFits(param, bound, own, colours)) return bound
  if (defaultFits(param, bound, param.initial, colours)) return rest as ListingBound
  const start = defaultFits(param, bound, current, colours) ? current : nearestAllowed(param, bound, current ?? own ?? param.initial, colours)
  return start === undefined || !defaultFits(param, bound, start, colours) ? (rest as ListingBound) : ({ ...rest, default: start } as ListingBound)
}

/** The colours a customer can pick on `draft`: `listingInput`'s colour choices (the listing's filaments but support-only ones and support materials). */
export function pickableColours(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): string[] {
  const { supportOnly } = withoutSupport(draft, schema, filaments)
  return filamentColours(draft, customerFilaments(filaments)).filter((colour) => !supportOnly.some((support) => sameColour(support, colour)))
}

function filamentColours(draft: ListingDraft, filaments: readonly Filament[]): string[] {
  return draft.colours.flatMap((id) => filaments.find((filament) => filament.id === id)?.colour ?? [])
}

/**
 * `draft`'s bounds with every support parameter locked at its default, and its support-only
 * filaments: a listing filament whose colour only a support parameter uses and whose material
 * is not the design's (the material of a listing filament a customer colour's value names;
 * when none is named, that of every listing filament whose colour no support parameter uses).
 * That is the filament `defaultsFor` adds for a support
 * colour outside the material pool; a filament of the design's material stays a choice
 * whatever a support setting uses. No offered colour takes a colour every listing filament of
 * which is support-only (`listingInput`), and the editor marks them (`isSupportOnly`).
 */
function withoutSupport(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]) {
  const byName = new Map((schema?.parameters ?? []).map((param) => [param.name, param]))
  const fixed = { ...draft.fixed }
  const params: Record<string, ListingBound> = {}
  for (const [name, bound] of Object.entries(draft.params)) {
    const param = byName.get(name)
    if (param && isSupport(param, bound.label)) {
      if (param.initial != null) fixed[name] = param.initial
    } else {
      params[name] = bound
    }
  }
  const valueOf = (param: Param) => (params[param.name] ? param.initial : (fixed[param.name] ?? param.initial))
  const colourParams = [...byName.values()].filter((param) => param.type === 'color')
  const support = (param: Param) => isSupport(param, draft.params[param.name]?.label)
  const customerValues = colourParams.filter((param) => !support(param)).map(valueOf)
  const supportValues = colourParams
    .filter(support)
    .map(valueOf)
    .filter((colour) => !customerValues.some((value) => sameColour(value, colour)))
  const listed = filaments.filter((filament) => draft.colours.includes(filament.id))
  const supportColoured = (filament: Filament) => supportValues.some((colour) => sameColour(colour, filament.colour))
  const named = listed.filter((filament) => customerValues.some((value) => sameColour(value, filament.colour)))
  const materials = new Set((named.length > 0 ? named : listed.filter((filament) => !supportColoured(filament))).map((filament) => filament.material))
  const supportOnlyFilaments = listed.filter((filament) => supportColoured(filament) && !materials.has(filament.material))
  const supportOnly = supportValues.filter((colour) => {
    const same = listed.filter((filament) => sameColour(filament.colour, colour))
    return same.length > 0 && same.every((filament) => supportOnlyFilaments.includes(filament))
  })
  return { byName, params, fixed, supportOnly, supportOnlyFilaments }
}

/** Whether `filament` is on the listing only for a support colour (`withoutSupport`): there for publish's check, never offered to a customer. */
export function isSupportOnly(filament: Filament, draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): boolean {
  return withoutSupport(draft, schema, filaments).supportOnlyFilaments.some((other) => other.id === filament.id)
}

/**
 * The body the page saves: a support parameter is never offered (`isSupport`; one an older
 * listing offers is locked at its default), and each offered colour parameter takes the
 * listing filaments' colours but the colour of a support-only filament (`withoutSupport`)
 * or of a support material (`customerFilaments`).
 * That filament stays in `colours`: publish checks every locked colour against the listing's
 * filaments (backend `checked_fixed`, 422 `listing_colour_unknown`), and the support colour
 * is locked.
 */
export function listingInput(draft: ListingDraft, schema: CustomizerSchema | undefined, filaments: readonly Filament[]): StorefrontListingInput {
  const { byName, params, fixed } = withoutSupport(draft, schema, filaments)
  // #6152: never the colour of a support material (`customerFilaments`), which publish
  // refuses on offer, even one the shop ticked by hand.
  const colours = pickableColours(draft, schema, filaments)
  for (const [name, bound] of Object.entries(params)) {
    // A colour's choices are the filaments'; its own label (#6137) and default (#6144) stay.
    if (byName.get(name)?.type === 'color')
      params[name] = {
        kind: 'choice',
        choices: colours,
        ...(bound.label ? { label: bound.label } : {}),
        ...(bound.default != null ? { default: bound.default } : {}),
      }
  }
  return { ...draft, params, fixed }
}

/**
 * `value` as JSON with every object's keys sorted and its null fields left out, so two equal
 * bodies compare equal whatever order the server wrote their keys in. A null field is an
 * absent one (#6290): the backend stores and answers each bound's unset `label`, `default`
 * and `step` as null, while `listingInput` leaves an unset one out.
 */
function canonical(value: unknown): string {
  return JSON.stringify(value, (_key, item: unknown) =>
    item !== null && typeof item === 'object' && !Array.isArray(item)
      ? Object.fromEntries(
          Object.entries(item as Record<string, unknown>)
            .filter(([, field]) => field !== null)
            .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)),
        )
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
export function unsaveable(body: StorefrontListingInput, schema?: CustomizerSchema): string | null {
  for (const [name, bound] of Object.entries(body.params ?? {})) {
    if (bound.kind === 'choice' && bound.choices.length === 0) {
      const label = schema?.parameters?.find((param) => param.name === name)?.caption || name
      return `“${label}” is offered with nothing to choose. Give the customer at least one choice (for a colour, tick a colour on offer that is not for supports only), or lock it.`
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
  /** The shop's default materials (`storefront_materials`), once read. */
  shop?: readonly string[],
): PublicListing {
  const input = listingInput(draft, schema, filaments)
  const params: Record<string, PublicParam> = {}
  for (const param of schema?.parameters ?? []) {
    const bound = input.params?.[param.name]
    if (!bound) continue
    // What an unpicked render takes (`effective_default`): the listing's, else the template's.
    // A choice shows it only while it is one of its choices, as `_public_param` does: a
    // colour's are the listing filaments' colours, support-only ones left out. A number
    // shows it only inside its range and on its step, the schema's when it has none (#6144).
    const start = bound.default ?? param.initial ?? null
    const allowed =
      bound.kind === 'choice'
        ? bound.choices.some((choice) => (param.type === 'color' ? sameColour(choice, start) : choice === start))
        : bound.kind !== 'number' || start === null || valueProblem({ ...bound, step: bound.step ?? param.step ?? null } as PublicParam, start) === null
    const shownDefault = allowed ? start : null
    const placeholder = bound.kind === 'text' && typeof start === 'string' && start.trim() ? start : null
    params[param.name] = {
      ...bound,
      // As the public page shows it (`shown_label`, `_public_param`).
      label: shownLabel(param.name, bound.label, param.caption),
      group: param.group ?? '',
      colour: param.type === 'color',
      default: shownDefault,
      ...(bound.kind === 'text' ? { placeholder } : {}),
    } as PublicParam
  }
  return {
    id: 'preview',
    title: draft.title || 'Untitled listing',
    description: draft.description,
    params,
    // #6152: never a support material, as the public listing (`customer_filaments`).
    colours: draft.colours.flatMap((id) => {
      const filament = customerFilaments(filaments).find((f) => f.id === id)
      return filament
        ? [
            {
              id,
              colour: filament.colour,
              colour_name: filament.colour_name,
              material: filament.material,
              alpha: filament.alpha,
              extra_colours: filament.extra_colours,
              effect: filament.effect,
            },
          ]
        : []
    }),
    // #6258: the listing's own, else the shop's default (`shop`, once read); empty until
    // then. A draft stored before #6258 has none: the shop's default too.
    materials: effectiveMaterials(draft, shop) ?? [],
    media: draft.media_ids.flatMap((id) => {
      const item = media.find((m) => m.id === id)
      return item ? [{ url: mediaUrl(item), kind: item.kind, caption: item.caption }] : []
    }),
    updated_at: '',
    // The preview does not judge which materials are printable (#6302 review): publish
    // refuses a listing none can print, and an empty colour still shows as gone.
    available: true,
  }
}
