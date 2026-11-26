import { api, ApiError } from '../../api/client'
import type { Filament, ParamValue, RevisionSchema } from '../../api/types'
import { inputFields, type InputField } from '../../lib/flowInputs'
import { isJsonObject, isParamValue, splitInputs, type JsonObject } from '../../lib/inputs'
import type { ParamValues } from '../../lib/params'
import { migrateIfOld } from '../../lib/useMigratedInputs'
import { fittedDefault, lockedValue, pickableColours, sameColour, shownParams, type DesignSchema, type ListingDraft, type ListingRefusal } from './listingDraft'

/**
 * #5701 (spec 2026-11-17 §5) — a pipeline template's own inputs on a listing: everything in
 * a render's inputs but `params` and `v`. The shop sets them, and no customer changes them.
 * The shape they have and their version are the listing's commit's (`RevisionSchema.pipeline`).
 */

export type Pipeline = NonNullable<RevisionSchema['pipeline']>

/** The pipeline the design has at the listing's commit; undefined for one with none, or not read yet. */
export function pipelineOf(schema: DesignSchema | undefined): Pipeline | undefined {
  return schema?.pipeline ?? undefined
}

/** Whether `draft` holds inputs that publish would check. */
export function hasInputs(draft: ListingDraft): boolean {
  return Object.keys(draft.inputs ?? {}).length > 0
}

/**
 * What a listing takes from a render's or an output's whole inputs (`whole`): the values its
 * smart defaults run over, and for a pipeline template the inputs and the version they were
 * made at. A template with no pipeline takes the values only, since publish refuses inputs
 * there. The shared sell-draft vectors (`source`) pin it.
 */
export function soldDesign(schema: DesignSchema, whole: Readonly<Record<string, unknown>>): { values: ParamValues; design: Pick<ListingDraft, 'inputs' | 'inputs_v'> } {
  const { params, extra } = splitInputs(whole)
  if (!pipelineOf(schema)) return { values: params, design: {} }
  const { v, ...inputs } = extra
  return { values: params, design: { inputs, inputs_v: version(v) ?? 0 } }
}

function version(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isInteger(v) && v >= 0 ? v : undefined
}

// ---------------------------------------------------------------- the dialog's working copy

/** The parameters a render of `draft` takes while the customer picks none: its locks, its starting values and the defaults. */
export function listingParams(schema: DesignSchema, draft: ListingDraft): ParamValues {
  const params: ParamValues = {}
  for (const param of schema.parameters ?? []) {
    const bound = draft.params[param.name]
    const value = bound ? (bound.default ?? param.initial) : lockedValue(draft, param)
    if (isParamValue(value)) params[param.name] = value
  }
  return params
}

/** What "Set up the design" opens on: the listing's whole inputs at the commit's version, and what to say about them. */
export interface DesignStart {
  inputs: JsonObject
  /** Why the listing's own inputs could not be taken, when they could not. */
  note: string | null
}

/**
 * The listing's inputs brought up to the commit's `INPUTS_VERSION`, as the Customize page
 * does when it opens an older output. A listing with none starts from the template's own
 * design: inputs of version 0 with no key, as the template's `migrate` fills them; a template
 * that fills none starts empty. Its `params` stay the listing's, whatever a migration makes
 * of them, as at publish (§4.3). Rejects when the render service did not answer.
 */
export async function designStart(schema: DesignSchema, draft: ListingDraft): Promise<DesignStart> {
  const current = pipelineOf(schema)?.inputs_version ?? 0
  const params = listingParams(schema, draft)
  const whole = (inputs: Readonly<Record<string, unknown>>, v: number): JsonObject => ({ ...(inputs as JsonObject), params, v })
  const { slug, commit } = draft

  const fresh = async (): Promise<JsonObject> => {
    const made = await migrated(slug, whole({}, 0), current, commit)
    return typeof made === 'string' ? whole({}, current) : { ...made, params }
  }

  if (!hasInputs(draft) && draft.inputs_v == null) return { inputs: await fresh(), note: null }
  const made = draft.inputs_v ?? current
  if (made > current) {
    return { inputs: await fresh(), note: 'This listing’s design was made with a newer version of the template. Set it up again for this version.' }
  }
  const now = await migrated(slug, whole(draft.inputs ?? {}, made), current, commit)
  if (typeof now !== 'string') return { inputs: { ...now, params }, note: null }
  return { inputs: await fresh(), note: `This listing’s design could not be brought up to this version of the template: ${now}. Set it up again.` }
}

/** `inputs` at `current`, or why the template refused them. Rejects when nothing answered. */
async function migrated(slug: string, inputs: JsonObject, current: number, commit: string): Promise<JsonObject | string> {
  if (inputs.v === current) return inputs
  try {
    const result = await api.migrateInputs(slug, inputs, commit)
    const now = isJsonObject(result.inputs) ? (result.inputs as JsonObject) : inputs
    return { ...now, v: current }
  } catch (error) {
    if (error instanceof ApiError && error.status === 422) return error.detail
    throw error
  }
}

/** A preset's whole inputs at the commit's version, or a rejection saying why not. */
export async function presetInputsAt(schema: DesignSchema, slug: string, commit: string, saved: JsonObject): Promise<JsonObject> {
  const outcome = await migrateIfOld(slug, saved, pipelineOf(schema)?.inputs_version ?? 0, commit)
  if (outcome.kind === 'failed') throw new Error(`this preset could not be brought up to this version: ${outcome.error}`)
  return outcome.inputs
}

/**
 * `draft` with the dialog's working copy (§5.3): its own inputs and their version, and each
 * of its `params` as the locked value of a locked parameter or the starting value of an
 * offered one. The bounds and the colours on offer do not change; a starting value they do
 * not allow becomes the nearest one they do. `params` and `fixed` stay the same objects when
 * nothing in them changed, so no untouched field reads as edited.
 */
export function withDesign(draft: ListingDraft, schema: DesignSchema, filaments: readonly Filament[], working: JsonObject): ListingDraft {
  const { params: values, v, ...inputs } = working
  const set = isJsonObject(values) ? values : {}
  const colours = pickableColours(draft, schema, filaments)
  const params = { ...draft.params }
  const fixed = { ...draft.fixed }
  let moved = false
  for (const param of shownParams(schema)) {
    const next = set[param.name] ?? param.initial
    if (!isParamValue(next)) continue
    const bound = draft.params[param.name]
    const now = bound ? (bound.default ?? param.initial) : lockedValue(draft, param)
    const same = (a: ParamValue | null | undefined) => (param.type === 'color' ? sameColour(next, a) : next === a)
    if (same(now)) continue
    moved = true
    if (bound) {
      const { default: _own, ...rest } = bound
      const start = same(param.initial) ? rest : { ...rest, default: next }
      params[param.name] = fittedDefault(param, start as typeof bound, colours, next)
    } else {
      fixed[param.name] = next
    }
  }
  return {
    ...draft,
    ...(moved ? { params, fixed } : {}),
    inputs,
    inputs_v: version(v) ?? pipelineOf(schema)?.inputs_version ?? null,
  }
}

/** `draft` with no design inputs: a version with no pipeline takes none. */
export function withoutDesign(draft: ListingDraft): ListingDraft {
  const { inputs: _inputs, inputs_v: _v, ...rest } = draft
  return rest
}

/** `draft` without the input `name` (`inputs.house.attic`) that this version does not have. */
export function withoutInput(draft: ListingDraft, name: string): ListingDraft {
  const drop = (node: Record<string, unknown>, [head = '', ...rest]: string[]): Record<string, unknown> => {
    const { [head]: child, ...others } = node
    if (rest.length === 0 || !isJsonObject(child)) return others
    return { ...others, [head]: drop(child, rest) }
  }
  return { ...draft, inputs: drop(draft.inputs ?? {}, pathOf(name)) }
}

// ---------------------------------------------------------------- what the editor says

type Spec = Readonly<Record<string, unknown>>

function specOf(schema: unknown, key: string): Spec | undefined {
  const properties = isJsonObject(schema) ? schema['properties'] : undefined
  if (!isJsonObject(properties) || !Object.hasOwn(properties, key)) return undefined
  const spec = properties[key]
  return isJsonObject(spec) ? spec : undefined
}

/** An input's title, else its name in words. */
function titleOf(spec: Spec | undefined, key: string): string {
  const title = spec?.['title']
  if (typeof title === 'string' && title) return title
  const words = key.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function valueText(value: unknown): string {
  if (typeof value === 'boolean') return value ? 'on' : 'off'
  if (Array.isArray(value)) return value.length > 0 ? value.map(valueText).join(', ') : 'none'
  return value === null ? 'none' : typeof value === 'object' ? 'set' : String(value)
}

/**
 * The draft's inputs in words, from the schema's titles, for a design whose template says
 * nothing of itself: "House: Modules wide 3, Modules deep 1". One the schema does not have
 * is named too, since publish refuses it.
 */
export function designText(schema: unknown, inputs: Readonly<Record<string, unknown>>): string {
  const keys = [...Object.keys(isJsonObject(schema) && isJsonObject(schema['properties']) ? schema['properties'] : {}), ...Object.keys(inputs)]
  return [...new Set(keys)]
    .filter((key) => Object.hasOwn(inputs, key))
    .map((key) => {
      const spec = specOf(schema, key)
      const value = inputs[key]
      if (!isJsonObject(value)) return `${titleOf(spec, key)}: ${valueText(value)}`
      const inner = Object.entries(value).map(([name, item]) => `${titleOf(specOf(spec, name), name)} ${valueText(item)}`)
      return `${titleOf(spec, key)}: ${inner.join(', ') || 'empty'}`
    })
    .join('. ')
}

type InputsRefusal = Extract<ListingRefusal, { kind: 'inputs' }>

function pathOf(name: string): string[] {
  return name.split('.').slice(1)
}

const TYPES: Readonly<Record<string, string>> = { integer: 'a whole number', number: 'a number', string: 'text', boolean: 'on or off', array: 'a list', object: 'set up again' }

/** Why a value of `spec` is refused, after its title. */
function reasonText(reason: string, spec: Spec): string | null {
  const limit = spec[reason]
  switch (reason) {
    case 'minimum':
      return `at least ${valueText(limit)}`
    case 'maximum':
      return `at most ${valueText(limit)}`
    case 'exclusiveMinimum':
      return `more than ${valueText(limit)}`
    case 'exclusiveMaximum':
      return `less than ${valueText(limit)}`
    case 'multipleOf':
      return `a multiple of ${valueText(limit)}`
    case 'minLength':
      return `at least ${valueText(limit)} characters`
    case 'maxLength':
      return `at most ${valueText(limit)} characters`
    case 'minItems':
      return `at least ${valueText(limit)} in the list`
    case 'maxItems':
      return `at most ${valueText(limit)} in the list`
    case 'uniqueItems':
      return 'the same one is in the list twice'
    case 'enum':
      return `one of ${valueText(limit)}`
    case 'pattern':
      return 'not in the form this design asks for'
    case 'type':
      return `must be ${TYPES[String(limit)] ?? 'another kind of value'}`
    default:
      return null
  }
}

function asSentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`
}

/** `detail` without the raw path it starts with, which no person reads. */
function withoutPath(detail: string, name: string): string {
  return detail.startsWith(`${name}: `) ? detail.slice(name.length + 2) : detail
}

/** What publish (or a save) refused about the design's inputs, with the schema's titles (`schema`), never a path. */
export function inputsRefusalText(refusal: InputsRefusal, schema: unknown): string {
  const { code, name, reason, detail } = refusal
  if (code === 'unexpected') return 'This version of the template has no design to set up. Clear the design, or choose another version.'
  if (code === 'undeclared') return asSentence(`This version of the template cannot be listed: ${withoutPath(detail, name)}`)
  if (code === 'migration_failed') return `${asSentence(`The design could not be brought up to this version of the template: ${withoutPath(detail, name)}`)} Set it up again.`
  if (reason === 'too_large') return 'The design holds too much to save. Set up a smaller one.'
  if (reason === 'unstorable') return 'The design holds text or a number that cannot be saved. Set it up again.'
  if (reason === 'newer') return 'The design was made with a newer version of the template. Set it up again, or choose that version.'

  const path = pathOf(name)
  let spec: Spec | undefined = isJsonObject(schema) ? schema : undefined
  let title = 'The design'
  let item: number | null = null
  for (const key of path) {
    if (spec?.['type'] === 'array' && /^\d+$/.test(key)) {
      item = Number(key) + 1
      const items: unknown = spec['items']
      spec = isJsonObject(items) ? items : undefined
      continue
    }
    spec = specOf(spec, key)
    title = titleOf(spec, key)
  }
  if (item !== null) title = `${title}, item ${item}`
  if (reason === 'required') return `${title}: not set. Set up the design.`
  if (reason === 'additional') return `${title}: this version of the template does not have it.`
  const why = reason !== null && spec ? reasonText(reason, spec) : null
  return asSentence(`${title}: ${why ?? withoutPath(detail, name)}`)
}

// ---------------------------------------------------------------- the schema's own form

/** The fields the schema's own form draws, in its order: a top-level value, or an object's under its title. */
export interface FieldGroup {
  key: string
  /** The object's title; null for a top-level value, which is a field of its own. */
  title: string | null
  description: string | null
  required: boolean
  fields: InputField[]
}

/** The form's fields, each named by its path (`house.cols`). `now` is what the listing has, else the schema's default. */
export function fieldGroups(schema: unknown): FieldGroup[] {
  const properties = isJsonObject(schema) && isJsonObject(schema['properties']) ? schema['properties'] : {}
  const tops = new Map(inputFields(schema).map((field) => [field.name, field]))
  const required = new Set(isJsonObject(schema) && Array.isArray(schema['required']) ? schema['required'] : [])
  return Object.entries(properties).flatMap(([key, spec]): FieldGroup[] => {
    const top = tops.get(key)
    if (top) return [{ key, title: null, description: null, required: top.required, fields: [labelled(top, key)] }]
    if (!isJsonObject(spec) || spec['type'] !== 'object') return []
    const needed = required.has(key)
    const fields = inputFields(spec).map((field) => ({ ...labelled(field, field.name), name: `${key}.${field.name}`, required: needed && field.required }))
    return [{ key, title: titleOf(spec, key), description: typeof spec['description'] === 'string' ? spec['description'] : null, required: needed, fields }]
  })
}

function labelled(field: InputField, key: string): InputField {
  return field.label === field.name ? { ...field, label: titleOf(undefined, key) } : field
}

/** What each field starts at: `inputs`' own value, else the schema's default. */
export function fieldValues(groups: readonly FieldGroup[], inputs: Readonly<Record<string, unknown>>): Record<string, unknown> {
  const values: Record<string, unknown> = {}
  for (const group of groups) {
    for (const field of group.fields) {
      const holder = group.title === null ? inputs : inputs[group.key]
      const key = field.name.slice(group.title === null ? 0 : group.key.length + 1)
      const own = isJsonObject(holder) && Object.hasOwn(holder, key) ? holder[key] : undefined
      const value = own ?? field.default
      if (value !== undefined) values[field.name] = value
    }
  }
  return values
}

/**
 * The inputs the form's `values` (by path) make, over `base`, the inputs it opened on: what the
 * form draws comes from its fields (a field left empty is left out), and what it cannot draw
 * (a deeper object, a list of objects) stays as it was. An object nobody needs stays out while
 * nothing in it is set.
 */
export function fieldInputs(groups: readonly FieldGroup[], values: Readonly<Record<string, unknown>>, base: Readonly<Record<string, unknown>> = {}): JsonObject {
  const inputs: JsonObject = { ...(base as JsonObject) }
  for (const group of groups) {
    const own = Object.fromEntries(group.fields.filter((field) => Object.hasOwn(values, field.name)).map((field) => [field.name, values[field.name]])) as JsonObject
    if (group.title === null) {
      for (const field of group.fields) delete inputs[field.name]
      Object.assign(inputs, own)
      continue
    }
    const inner = Object.fromEntries(Object.entries(own).map(([path, value]) => [path.slice(group.key.length + 1), value])) as JsonObject
    const before = base[group.key]
    const drawn = new Set(group.fields.map((field) => field.name.slice(group.key.length + 1)))
    const kept = isJsonObject(before) ? Object.fromEntries(Object.entries(before).filter(([key]) => !drawn.has(key))) : {}
    delete inputs[group.key]
    if (group.required || Object.keys(kept).length > 0 || Object.values(inner).some((value) => value !== false)) inputs[group.key] = { ...(kept as JsonObject), ...inner }
  }
  return inputs
}
