import type { CustomizerSchema, Filament, ListingBound, MediaView, OutputPlate, Param, ParamValue, RevisionSchema, SlotNeed, StorefrontListing, StorefrontListingInput } from '../../api/types'
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
  /**
   * #6710 — what the listing sells: a template's commit (`slug` and `commit`, the default),
   * or a library file (`library_file`). Absent is a template, as before.
   */
  source?: 'template' | 'library_file'
  /** #6710 — the file a `library_file` listing sells; null until one is picked. */
  library_file?: DraftFile | null
  /**
   * #5701 — a pipeline template's own inputs and the version they were made at, which the
   * shop sets in "Set up the design" (`designInputs.ts`) or by "Sell on storefront". A save
   * replaces the whole listing, so both go with every one. Another template starts with none
   * (`emptyDraft`).
   */
  inputs?: Record<string, unknown>
  inputs_v?: number | null
}

/** A design's schema at a commit, with what that revision declares of its UI and its pipeline. */
export type DesignSchema = CustomizerSchema & Partial<Pick<RevisionSchema, 'ui' | 'ui_error' | 'pipeline' | 'pipeline_params'>>

/**
 * #6710 §4.1 — the library file a draft sells. `pinned` is the sha256 of the version the
 * listing sells (null before its first publish, and once another file is picked); the
 * server sets it, so a save never sends it. `repin` asks the next publish to take the
 * library's version ("Use the library's version").
 */
export interface DraftFile {
  id: number
  library: 'bambuddy' | 'native'
  filename: string
  repin: boolean
  pinned: string | null
}

/** One part of a file a listing sells (`FileSlot`): read by publish, or from the file before it. */
export interface DraftSlot {
  /** From 0. */
  slot: number
  colour: string
  material: string | null
  used: boolean
  support: boolean
}

/** Whether `draft` sells a library file (#6710). */
export function isFileDraft(draft: ListingDraft): boolean {
  return draft.source === 'library_file'
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
  const file = listing.library_file
  return {
    // A file listing (#6710) has no template.
    ...(listing.source === 'library_file'
      ? {
          source: 'library_file' as const,
          library_file: file ? { id: file.id, library: file.library, filename: file.filename ?? '', repin: file.repin, pinned: file.pinned ?? null } : null,
        }
      : {}),
    slug: listing.slug ?? '',
    commit: listing.commit ?? '',
    title: listing.title,
    description: listing.description,
    params: listing.params,
    fixed: listing.fixed,
    colours: listing.colours,
    media_ids: listing.media_ids,
    // A listing stored before #6258 has none: the shop's default.
    materials: (listing.materials as string[] | undefined) ?? [],
    // #5701. A listing stored before it has none, as a template with no pipeline has none.
    // Inputs with no key still count with their version: publish migrates them from it.
    ...(Object.keys((listing.inputs as StorefrontListing['inputs'] | undefined) ?? {}).length > 0 || listing.inputs_v != null
      ? { inputs: listing.inputs, inputs_v: listing.inputs_v }
      : {}),
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
  | { kind: 'param'; code: ParamRefusalCode; name: string; detail: string }
  /** 422 `listing_colour_unknown` naming no parameter: a filament on the listing is no longer in the price book. */
  | { kind: 'filament_unknown'; detail: string }
  /**
   * #6710 §9.6 — a refusal about a file listing: a `listing_file_*` code or
   * `listing_source_invalid`, said in the Source block or under "Colours on offer", as
   * `name` (the field) says.
   */
  | { kind: 'file'; code: string; name: string; detail: string }
  /**
   * #5701 — a refusal about the design's own inputs (422 `listing_inputs_*`), said in the
   * Design block: `name` is `inputs` or one input's path, `reason` the check it failed
   * (`designInputs.ts` `inputsRefusalText`).
   */
  | { kind: 'inputs'; code: InputsRefusalCode; name: string; reason: string | null; detail: string }

/** `pipeline_set` (#5701): 422 `listing_param_pipeline_set`, a parameter the design's pipeline sets itself. */
type ParamRefusalCode = 'default_invalid' | 'colour_unknown' | 'support_offered' | 'pipeline_set'
const PARAM_REFUSALS = ['default_invalid', 'colour_unknown', 'support_offered'] as const
const INPUTS_REFUSALS = ['unexpected', 'undeclared', 'reserved', 'invalid', 'migration_failed'] as const
type InputsRefusalCode = (typeof INPUTS_REFUSALS)[number]

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
  const slug = problem.type?.startsWith(STOREFRONT_PROBLEMS) ? problem.type.slice(STOREFRONT_PROBLEMS.length) : ''
  if (slug.startsWith('listing_file_') || slug === 'listing_source_invalid') {
    return { kind: 'file', code: slug, name: typeof problem['name'] === 'string' ? problem['name'] : '', detail }
  }
  const name = typeof problem['name'] === 'string' ? problem['name'] : ''
  const inputs = INPUTS_REFUSALS.find((found) => slug === `listing_inputs_${found}`)
  if (inputs !== undefined) return { kind: 'inputs', code: inputs, name: name || 'inputs', reason: typeof problem['reason'] === 'string' ? problem['reason'] : null, detail }
  // Its `name` is the field's path, `params.<name>`.
  if (slug === 'listing_param_pipeline_set' && name) return { kind: 'param', code: 'pipeline_set', name: name.replace(/^params\./, ''), detail }
  return null
}

/** What publish refused about parameter `label` (`refusal`), said beside it. */
export function refusalText(refusal: Extract<ListingRefusal, { kind: 'param' }>, label: string): string {
  if (refusal.code === 'default_invalid') return `Not published: “${label}” starts at a value these choices do not allow. Set “Starts at” to one they allow.`
  if (refusal.code === 'colour_unknown') return `Not published: “${label}” uses a colour that no colour on offer has. Pick a colour on offer, or tick a filament of that colour under “Colours on offer”.`
  if (refusal.code === 'pipeline_set') return `Not published: the design sets “${label}” itself, so a listing cannot.`
  return `Not published: “${label}” sets support, which stays yours and a customer never sees. Turn off “Customer can change”.`
}

/** The parameters a listing can name: every one outside the hidden group that the pipeline does not set itself (#5701). */
export function shownParams(schema: DesignSchema | undefined): Param[] {
  const set = schema?.pipeline_params ?? []
  return (schema?.parameters ?? []).filter((param) => param.group !== HIDDEN_GROUP && !set.includes(param.name))
}

/**
 * `draft`'s bounds and locks that `schema` takes: none of a parameter it does not have (a
 * version without it), and none of one its pipeline sets itself (#5701) but a lock at the
 * default, which publish takes. The editor shows no row for either, so a person could not
 * undo what publish would refuse. `draft` itself when every one fits.
 */
export function withoutUnknown(draft: ListingDraft, schema: DesignSchema): ListingDraft {
  const set = schema.pipeline_params ?? []
  const byName = new Map((schema.parameters ?? []).map((param) => [param.name, param]))
  const offered = (name: string) => byName.has(name) && !set.includes(name)
  // As publish compares a lock with the default: a colour in any case.
  const atDefault = (param: Param, value: ParamValue) => (param.type === 'color' ? sameColour(value, param.initial) : value === param.initial)
  const locked = (name: string, value: ParamValue) => {
    const param = byName.get(name)
    return param !== undefined && (!set.includes(name) || atDefault(param, value))
  }
  const { params, fixed } = draft
  if (Object.keys(params).every(offered) && Object.entries(fixed).every(([name, value]) => locked(name, value))) return draft
  return {
    ...draft,
    params: Object.fromEntries(Object.entries(params).filter(([name]) => offered(name))),
    fixed: Object.fromEntries(Object.entries(fixed).filter(([name, value]) => locked(name, value))),
  }
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
  schema: DesignSchema,
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
export function colourProblems(draft: ListingDraft, schema: DesignSchema | undefined, filaments: readonly Filament[]): ColourProblem[] {
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
export function titleColours(draft: ListingDraft, schema: DesignSchema | undefined, filaments: readonly Filament[]): string[] {
  // #6710: a file listing's every part takes any colour on offer.
  const offered = isFileDraft(draft)
    ? pickableColours(draft, undefined, filaments)
    : Object.values(listingInput(draft, schema, filaments).params ?? {}).flatMap((bound) => (bound.kind === 'choice' ? bound.choices : []))
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
export function pickableColours(draft: ListingDraft, schema: DesignSchema | undefined, filaments: readonly Filament[]): string[] {
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
function withoutSupport(draft: ListingDraft, schema: DesignSchema | undefined, filaments: readonly Filament[]) {
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
export function isSupportOnly(filament: Filament, draft: ListingDraft, schema: DesignSchema | undefined, filaments: readonly Filament[]): boolean {
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
export function listingInput(draft: ListingDraft, schema: DesignSchema | undefined, filaments: readonly Filament[]): StorefrontListingInput {
  if (isFileDraft(draft)) return fileInput(draft)
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
  const draft = draftOf(listing)
  return canonical(isFileDraft(draft) ? fileInput(draft) : draft)
}

/**
 * #6710 §4.1 — a file listing's body: the file and the stored request (`repin`), never the
 * pin (the server's) or a template's fields. A draft with no file picked yet sends none,
 * which the server refuses as `listing_source_invalid`.
 */
function fileInput(draft: ListingDraft): StorefrontListingInput {
  const file = draft.library_file
  return {
    source: 'library_file',
    ...(file ? { library_file: { id: file.id, library: file.library, repin: file.repin } } : {}),
    title: draft.title,
    description: draft.description,
    params: {},
    fixed: {},
    colours: draft.colours,
    media_ids: draft.media_ids,
    materials: draft.materials,
  }
}

/**
 * #6710 §9.1 — `draft` switched to sell `source`: what the other source set (a template's
 * design, version and settings, or the file) and the pictures, whose ids name the other
 * source's, are dropped; the title, the description, the colours and the materials stay.
 */
export function switchSource(draft: ListingDraft, source: 'template' | 'library_file'): ListingDraft {
  const kept = { title: draft.title, description: draft.description, colours: draft.colours, materials: draft.materials }
  return source === 'library_file' ? { ...emptyDraft(), ...kept, source, library_file: null } : { ...emptyDraft(), ...kept }
}

/** Whether `material` is a support material, which a customer never picks (`customerFilaments`). */
export function isSupportMaterial(material: string | null | undefined): boolean {
  return typeof material === 'string' && customerFilaments([{ material }]).length === 0
}

/**
 * #6710 §9.3 — a file listing's title at first: the file's name without its ending, its
 * underscores read as spaces, and without the names of the colours a customer can pick
 * (the owner's rule: no colour in a title).
 */
export function fileTitle(filename: string, filaments: readonly Filament[]): string {
  const name = filename.replace(/(\.gcode)?\.[a-z0-9]+$/i, '').replace(/_+/g, ' ')
  const colours = customerFilaments(filaments).flatMap((filament) => (filament.colour_name ? [filament.colour_name] : []))
  return withoutWords(name, colours).slice(0, 200) || name.slice(0, 200)
}

/**
 * #6710 — the file's declared slots (`GET /print/library/{id}/filaments` `slots`) as its parts
 * before a publish: every one counted as used, a support material's as support, one with no
 * colour grey. The agent's `storefrontDraft.ts` keeps a copy (the shared vectors).
 */
export function declaredSlots(slots: readonly SlotNeed[]): DraftSlot[] {
  return slots.map((slot) => ({
    slot: slot.slot_id - 1,
    colour: slot.colour || '#808080',
    material: slot.material ?? null,
    used: true,
    support: isSupportMaterial(slot.material),
  }))
}

/** #6710 §9.3 — the picture ids (`plate:<n>`) of the file's plates that have a picture, in plate order. */
export function platePictures(plates: readonly OutputPlate[]): string[] {
  return plates.filter((plate) => plate.has_thumbnail).map((plate) => `plate:${plate.index}`)
}

/**
 * #6710 — the filaments a file listing offers at first: the priced customer filaments of the
 * material whose colour one of the offered parts has (`offeredFilaments`), and, for each
 * support part, the support filament publish would lock it to.
 */
export function fileColours(slots: readonly DraftSlot[], filaments: readonly Filament[]): string[] {
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
 * #6710 §5.1 row 8 — the support filament publish locks support part `slot` to: the first of
 * the listing's support filaments (`colours`, in order) of the part's material, else its
 * first support filament; undefined when the listing has none, which publish refuses.
 */
export function supportFilament(slot: DraftSlot, colours: readonly string[], filaments: readonly Filament[]): Filament | undefined {
  const supports = colours.flatMap((id) => filaments.find((filament) => filament.id === id && isSupportMaterial(filament.material)) ?? [])
  const same = supports.filter((filament) => slot.material !== null && materialKey(filament.material) === materialKey(slot.material))
  return same[0] ?? supports[0]
}

/**
 * Why `body` cannot be saved, or null: a parameter offered with nothing to choose (most
 * often a colour parameter with every filament unchecked), which the backend refuses as
 * 422 `listing_choice_empty`.
 */
export function unsaveable(body: StorefrontListingInput, schema?: DesignSchema): string | null {
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
  schema: DesignSchema | undefined,
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
    source: 'template',
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
